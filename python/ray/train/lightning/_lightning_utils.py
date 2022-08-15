from typing import TYPE_CHECKING, Optional, Dict, Type, Tuple, Any, Union
from inspect import isclass

import pytorch_lightning
from torch.utils.data import IterableDataset

from ray.air import session
from ray.air.constants import MODEL_KEY, PREPROCESSOR_KEY
from ray.air.checkpoint import Checkpoint
from ray.data.preprocessor import Preprocessor

if TYPE_CHECKING:
    from ray.data.dataset import Dataset
    import argparse

MODEL_PARAMS_KEY = f"{MODEL_KEY}_params"
MODEL_CLS_KEY = f"{MODEL_KEY}_cls"


class TorchIterableDataset(IterableDataset):
    def __init__(self, iterator):
        self.iterator = iterator

    def __iter__(self):
        yield from self.iterator


def process_datasets(
    train_dataset: "Dataset",
    val_dataset: "Dataset",
    test_dataset: "Dataset",
    predict_dataset: "Dataset",
    batch_size: Optional[int] = None,
) -> pytorch_lightning.LightningDataModule:
    """Convert Ray dataset shards to a PTL DataModule."""

    torch_datasets = {
        "train_dataset": TorchIterableDataset(train_dataset.iter_torch_batches())
    }
    if val_dataset:
        torch_datasets["val_dataset"] = TorchIterableDataset(
            val_dataset.iter_torch_batches()
        )
    if test_dataset:
        torch_datasets["test_dataset"] = TorchIterableDataset(
            test_dataset.iter_torch_batches()
        )
    if predict_dataset:
        torch_datasets["predict_dataset"] = TorchIterableDataset(
            predict_dataset.iter_torch_batches()
        )
    return pytorch_lightning.LightningDataModule.from_datasets(
        **torch_datasets, batch_size=batch_size, num_workers=0
    )


class TrainReportCheckpointLogger(pytorch_lightning.loggers.Logger):
    def __init__(self, model: pytorch_lightning.LightningModule, model_params: Dict):
        super().__init__()
        self._model = model
        self._model_params = model_params

    @property
    def name(self):
        return "TrainReportCheckpointLogger"

    @property
    def version(self):
        return session.get_trial_name()

    @pytorch_lightning.utilities.rank_zero_only
    def log_hyperparams(self, param: "argparse.Namespace"):
        pass

    def log_metrics(
        self, metrics: Dict[str, float], step: Optional[int] = None
    ) -> None:
        checkpoint = Checkpoint.from_dict(
            {
                MODEL_KEY: self._model.state_dict(),
                MODEL_PARAMS_KEY: self._model_params,
            }
        )
        session.report(metrics, checkpoint=checkpoint)


def load_checkpoint(
    checkpoint: Checkpoint,
    pl_module: Union[Type[pytorch_lightning.LightningModule], None],
) -> Tuple[Any, Optional["Preprocessor"]]:
    """Load a Ray Train Checkpoint.

    If the checkpoint was created using
    ``LightningCheckpoint.from_checkpoint(ckpt)`` then ``model`` can be ``None``.
    If the checkpoint was created from the result of ``trainer.fit()`` then you
    should pass a reference to your LightningModule subclass.

    Args:
        checkpoint: The checkpoint to load the weights and
            preprocessor from.
        pl_module: LightningModule subclass (not an instance) to use.

    Returns:
        The model and AIR preprocessor.
    """
    checkpoint_dict = checkpoint.to_dict()
    preprocessor = checkpoint_dict.get(PREPROCESSOR_KEY, None)
    required_keys = [MODEL_KEY, f"{MODEL_KEY}_params"]
    for required_key in required_keys:
        if MODEL_KEY not in checkpoint_dict:
            raise RuntimeError(
                f"No item with key: {required_key} is found in the "
                f"Checkpoint. Make sure this key exists when saving the "
                f"checkpoint in ``LightningTrainer``."
            )
    if pl_module is None:
        if MODEL_CLS_KEY in checkpoint_dict:
            pl_module = checkpoint_dict[MODEL_CLS_KEY]
        else:
            raise ValueError(
                "You must pass `pl_module` to `load_checkpoint` (or `model` to "
                f"`get_model`) if your checkpoint does not have the {MODEL_CLS_KEY} "
                "key in the checkpoint (e.g., if the checkpoint was saved during "
                "`trainer.fit()`)."
            )
    if isclass(pl_module):
        model_params = checkpoint_dict[MODEL_PARAMS_KEY]
        pl_module = pl_module(**model_params)
    pl_module.load_state_dict(checkpoint_dict[MODEL_KEY])
    return pl_module, preprocessor
