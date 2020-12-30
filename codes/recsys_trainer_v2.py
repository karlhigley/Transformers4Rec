import collections
from typing import Dict, Optional, Tuple, List, Any, Union

import torch
import math
import numpy as np
from torch.utils.data.dataset import Dataset
from torch import nn
from torch.utils.data.dataloader import DataLoader
from torch.cuda.amp import autocast
from copy import deepcopy

from transformers.integrations import (  
    is_fairscale_available
)

from transformers import (Trainer, AdamW,
                       get_constant_schedule_with_warmup, get_linear_schedule_with_warmup, get_cosine_schedule_with_warmup,
                       EvalPrediction, is_torch_tpu_available)
from transformers.trainer_utils import PredictionOutput
from transformers.utils import logging
from transformers.trainer_pt_utils import DistributedTensorGatherer, nested_concat
from transformers.trainer_callback import TrainerCallback
from enum import Enum

from recsys_metrics import EvalMetrics


from collections.abc import Sized

if is_fairscale_available():
    from fairscale.optim import OSS

logger = logging.get_logger(__name__)    

class DatasetType(Enum):
    train = "train"
    eval = "eval"


#Mock to inform HF Trainer that the dataset is sized, and can be obtained via the data loader
class DatasetMock(Dataset, Sized):

    def __init__(self, nsteps=0):
        self.nsteps = nsteps

    def __len__(self):
        return self.nsteps


class RecSysTrainerCallback(TrainerCallback):
    """
    A :class:`~transformers.TrainerCallback` that evaluates the model after each train epoch
    """

    def __init__(self, trainer: Trainer):
        self.trainer = trainer

    def on_train_begin(self, args, state, control, model=None, **kwargs):        
        #if args.dataloader_drop_last and args.max_steps < 0:
        #    state.max_steps -= 1
        pass

    def on_train_end(self, args, state, control, model=None, **kwargs):
        #Increments the global steps for logging with the global steps of the last train()
        self.trainer._increment_past_global_steps(state.global_step)

    def on_epoch_end(self, args, state, control, model=None, **kwargs):
        #Evaluates on eval set
        #self.trainer.evaluate()
        pass


class RecSysTrainer(Trainer):
    
    def __init__(self, *args, **kwargs):
        self.past_global_steps = 0

        if 'log_predictions' not in kwargs:
            self.log_predictions = False
        else:
            self.log_predictions = kwargs.pop('log_predictions')

        self.create_metrics()

        recsys_callback = RecSysTrainerCallback(self)

        mock_dataset = DatasetMock()
        super(RecSysTrainer, self).__init__(train_dataset=mock_dataset, 
                                            eval_dataset=mock_dataset,
                                            callbacks=[
                                                recsys_callback
                                                ],
                                            *args, **kwargs)

    def _increment_past_global_steps(self, current_global_step):
        self.past_global_steps += current_global_step

    def _get_past_global_steps(self):
        return self.past_global_steps

    def create_metrics(self):
        self.streaming_metrics_all = {}
        for dataset_type in DatasetType:
            self.streaming_metrics_all[dataset_type.value] = EvalMetrics(ks=[5,10,100,1000])


    def get_train_dataloader(self) -> DataLoader:
        return self.train_dataloader            
        
    def get_eval_dataloader(self, eval_dataset) -> DataLoader:
        return self.eval_dataloader

    def set_train_dataloader(self, dataloader):
        self.train_dataloader = dataloader
        
    def set_eval_dataloader(self, dataloader):
        self.eval_dataloader = dataloader

    def num_examples(self, dataloader):
        if dataloader == self.get_train_dataloader():
            batch_size = self.args.per_device_train_batch_size
        else:
            batch_size = self.args.per_device_eval_batch_size

        return len(dataloader) * batch_size

    def create_optimizer_and_scheduler(self, num_training_steps: int):
        """
        Setup the optimizer and the learning rate scheduler.

        We provide a reasonable default that works well. If you want to use something else, you can pass a tuple in the
        Trainer's init through :obj:`optimizers`, or subclass and override this method in a subclass.
        """
        if self.optimizer is None:
            no_decay = ["bias", "LayerNorm.weight"]
            optimizer_grouped_parameters = [
                {
                    "params": [p for n, p in self.model.named_parameters() if not any(nd in n for nd in no_decay)],
                    "weight_decay": self.args.weight_decay,
                },
                {
                    "params": [p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)],
                    "weight_decay": 0.0,
                },
            ]
            if self.sharded_dpp:
                self.optimizer = OSS(
                    params=optimizer_grouped_parameters,
                    optim=AdamW,
                    lr=self.args.learning_rate,
                    betas=(self.args.adam_beta1, self.args.adam_beta2),
                    eps=self.args.adam_epsilon,
                )
            else:
                self.optimizer = AdamW(
                    optimizer_grouped_parameters,
                    lr=self.args.learning_rate,
                    betas=(self.args.adam_beta1, self.args.adam_beta2),
                    eps=self.args.adam_epsilon,
                )

        if self.lr_scheduler is None:
            if self.args.learning_rate_schedule == 'constant_with_warmup':
                self.lr_scheduler = get_constant_schedule_with_warmup(self.optimizer, num_warmup_steps = self.args.learning_rate_warmup_steps)
            elif self.args.learning_rate_schedule == 'linear_with_warmup':
                self.lr_scheduler = get_linear_schedule_with_warmup(self.optimizer, num_warmup_steps = self.args.learning_rate_warmup_steps, num_training_steps=num_training_steps)
            elif self.args.learning_rate_schedule == 'cosine_with_warmup':
                self.lr_scheduler = get_cosine_schedule_with_warmup(self.optimizer, num_warmup_steps = self.args.learning_rate_warmup_steps, num_training_steps=num_training_steps,
                                                                    num_cycles= self.args.learning_rate_num_cosine_cycles_by_epoch * self.args.num_train_epochs)
            else:
                raise ValueError('Invalid value for --learning_rate_schedule.  Valid values: constant_with_warmup | linear_with_warmup | cosine_with_warmup')


    def log(self, logs: Dict[str, float]) -> None:
        """
        Log :obj:`logs` on the various objects watching training.

        Subclass and override this method to inject custom behavior.

        Args:
            logs (:obj:`Dict[str, float]`):
                The values to log.
        """
        if self.state.epoch is not None:
            logs["epoch"] = self.state.epoch

        #Incremental global steps across train() calls so that logs to W&B and Tensorboard do not overlap
        state_copy = deepcopy(self.state)
        if self.model.training:
            state_copy.global_step += self._get_past_global_steps()
        else:
            state_copy.global_step = self._get_past_global_steps()

        self.control = self.callback_handler.on_log(self.args, state_copy, self.control, logs)
        output = {**logs, **{"step": state_copy.global_step}}
        self.state.log_history.append(output)


    def prediction_loop(
        self,
        dataloader: DataLoader,
        description: str,
        prediction_loss_only: Optional[bool] = None,
        ignore_keys: Optional[List[str]] = None,
        metric_key_prefix: str = "eval",
    ) -> PredictionOutput:
        """
        Prediction/evaluation loop, shared by :obj:`Trainer.evaluate()` and :obj:`Trainer.predict()`.
        """

        #Ensures that metrics will be computed, even if self.compute_metrics function is not defined (because here we use streaming metrics)
        prediction_loss_only = False
        
        # Reseting streaming metrics for the dataset (Train, Valid or Test
        streaming_metrics_all_ds = self.streaming_metrics_all[metric_key_prefix]
        streaming_metrics_all_ds.reset()


        if not isinstance(dataloader.dataset, collections.abc.Sized):
            raise ValueError("dataset must implement __len__")
        prediction_loss_only = (
            prediction_loss_only if prediction_loss_only is not None else self.args.prediction_loss_only
        )

        model = self.model
        # multi-gpu eval
        if self.args.n_gpu > 1 and not self.args.model_parallel:
            model = torch.nn.DataParallel(model)
        # Note: in torch.distributed mode, there's no point in wrapping the model
        # inside a DistributedDataParallel as we'll be under `no_grad` anyways.

        batch_size = dataloader.batch_size
        
        num_examples = self.num_examples(dataloader)
        num_steps = int(math.ceil(num_examples / batch_size))
        '''
        if self.args.dataloader_drop_last:
            #Ignoring the last batch
            num_steps -= 1 
            #Fixing the number of examples after ignoring the last batch
            num_examples = num_steps * batch_size
        '''

        logger.info("***** Running %s *****", description)
        logger.info("  Num examples = %d", num_examples)
        logger.info("  Batch size = %d", batch_size)

        
        preds_item_ids_scores_host: Union[torch.Tensor, List[torch.Tensor]] = None
        labels_host: Union[torch.Tensor, List[torch.Tensor]] = None

        losses = []

        world_size = 1
        if is_torch_tpu_available():
            world_size = xm.xrt_world_size()
        elif self.args.local_rank != -1:
            world_size = torch.distributed.get_world_size()
        world_size = max(1, world_size)

        PADDING_INDEX = -100

        if not prediction_loss_only:
            preds_item_ids_scores_gatherer = DistributedTensorGatherer(world_size, num_examples, make_multiple_of=batch_size,
                                                                       padding_index=PADDING_INDEX)
            labels_gatherer = DistributedTensorGatherer(world_size, num_examples, make_multiple_of=batch_size,
                                                        padding_index=PADDING_INDEX)

        model.eval()

        if is_torch_tpu_available():
            dataloader = pl.ParallelLoader(dataloader, [self.args.device]).per_device_loader(self.args.device)

        if self.args.past_index >= 0:
            self._past = None

        self.callback_handler.eval_dataloader = dataloader

        for step, inputs in enumerate(dataloader):
            # Ignoring last training batch  if --dataloader_drop_last, because some data loaders does not support drop_last=True
            #if self.args.dataloader_drop_last and step+1 > num_steps:
            #    break 

            #Limits the number of evaluation steps on train set (which is usually larger)
            if metric_key_prefix == DatasetType.train.value and \
                self.args.eval_steps_on_train_set > 0 and step+1 > self.args.eval_steps_on_train_set:
                break
            
            loss, preds, labels, outputs = self.prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)

            losses.append(loss.item())

            if step % self.args.compute_metrics_each_n_steps == 0:

                #Updates metrics and returns detailed metrics if log_predictions=True
                metrics_results_detailed_all = None
                if streaming_metrics_all_ds is not None:
                    metrics_results_detailed_all = streaming_metrics_all_ds.update(preds, labels, return_individual_metrics=self.log_predictions)

            if labels is not None:
                labels_host = labels if labels_host is None else nested_concat(labels_host, labels, padding_index=-100)

            if preds is not None:
                preds_sorted_item_scores, preds_sorted_item_ids = torch.sort(preds, axis=1, descending=True)

                if self.args.predict_top_k > 0:
                    preds_sorted_item_scores = preds_sorted_item_scores[:, :self.args.predict_top_k]
                    preds_sorted_item_ids = preds_sorted_item_ids[:, :self.args.predict_top_k]

                # The output predictions will be a tuple with the ranked top-n item ids, and item recommendation scores
                preds_item_ids_scores = (preds_sorted_item_ids, preds_sorted_item_scores)                

                preds_item_ids_scores_host = preds_item_ids_scores if preds_item_ids_scores_host is None \
                                              else nested_concat(preds_item_ids_scores_host, preds_item_ids_scores, padding_index=-100)                
            
            self.control = self.callback_handler.on_prediction_step(self.args, self.state, self.control)

            # Gather all tensors and put them back on the CPU if we have done enough accumulation steps.
            if self.args.eval_accumulation_steps is not None and (step + 1) % self.args.eval_accumulation_steps == 0:
                #eval_losses_gatherer.add_arrays(self._gather_and_numpify(losses_host, "eval_losses"))
                if not prediction_loss_only:
                    preds_item_ids_scores_gatherer.add_arrays(self._gather_and_numpify(preds_item_ids_scores_host, "preds_item_ids_scores"))
                    labels_gatherer.add_arrays(self._gather_and_numpify(labels_host, "eval_label_ids"))

                # Set back to None to begin a new accumulation
                losses_host, preds_item_ids_scores_host, labels_host = None, None, None

        if self.args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of the evaluation loop
            delattr(self, "_past")

        # Gather all remaining tensors and put them back on the CPU
        if not prediction_loss_only:
            preds_item_ids_scores_gatherer.add_arrays(self._gather_and_numpify(preds_item_ids_scores_host, "preds_item_ids_scores"))
            labels_gatherer.add_arrays(self._gather_and_numpify(labels_host, "eval_label_ids"))

        preds_item_ids_scores = preds_item_ids_scores_gatherer.finalize() if not prediction_loss_only else None
        label_ids = labels_gatherer.finalize() if not prediction_loss_only else None

        #Truncating labels and predictions (because the last batch is usually not complete)
        valid_preds_mask = (label_ids != PADDING_INDEX)
        label_ids = label_ids[valid_preds_mask]
        if isinstance(preds_item_ids_scores, tuple):            
            preds_item_ids_scores = tuple([pred_section[valid_preds_mask] for pred_section in preds_item_ids_scores])
        else:
            preds_item_ids_scores = preds_item_ids_scores[valid_preds_mask]

        #if self.compute_metrics is not None and preds is not None and label_ids is not None:
        #    metrics = self.compute_metrics(EvalPrediction(predictions=preds, label_ids=label_ids))
        #else:
        #    metrics = {}

        metrics = {}

        #Computing the metrics results as the average of all steps
        if streaming_metrics_all_ds is not None:
            streaming_metrics_results_all = streaming_metrics_all_ds.result()
            metrics = {**metrics, **streaming_metrics_results_all}

        metrics[f"{metric_key_prefix}_loss"] = np.mean(losses)

        # Prefix all keys with metric_key_prefix + '_'
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

        return PredictionOutput(predictions=preds_item_ids_scores, label_ids=label_ids, metrics=metrics)


    def prediction_step(
        self,
        model: nn.Module,
        inputs: Dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[float], Optional[torch.Tensor], Optional[torch.Tensor], Optional[Dict[str, Any]]]:
        """
        Perform an evaluation step on :obj:`model` using obj:`inputs`.

        Subclass and override to inject custom behavior.

        Args:
            model (:obj:`nn.Module`):
                The model to evaluate.
            inputs (:obj:`Dict[str, Union[torch.Tensor, Any]]`):
                The inputs and targets of the model.

                The dictionary will be unpacked before being fed to the model. Most models expect the targets under the
                argument :obj:`labels`. Check your model's documentation for all accepted arguments.
            prediction_loss_only (:obj:`bool`):
                Whether or not to return the loss only.
            ignore_keys (:obj:`Lst[str]`, `optional`):
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions.

        Return:
            Tuple[Optional[float], Optional[torch.Tensor], Optional[torch.Tensor]]: A tuple with the loss, logits and
            labels (each being optional).
        """
        
        inputs = self._prepare_inputs(inputs)
        if ignore_keys is None:
            if hasattr(self.model, "config"):
                ignore_keys = getattr(self.model.config, "keys_to_ignore_at_inference", [])
            else:
                ignore_keys = []

        with torch.no_grad():
            if self.use_amp:
                with autocast():
                    outputs = model(**inputs)
            else:
                outputs = model(**inputs)

            loss = outputs["loss"].mean().detach()            

        if prediction_loss_only:
            return (loss, None, None, None)

        predictions = outputs["predictions"].detach()     
        labels = outputs["labels"].detach()     

        other_outputs = {k: v.detach() if isinstance(v, torch.Tensor) 
                                       else v for k, v in outputs.items()  if k not in ignore_keys + ["loss", "predictions", "labels"]}    

        return (loss, predictions, labels, other_outputs)