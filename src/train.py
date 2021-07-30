import json
import os
from dataclasses import dataclass, asdict, replace
from functools import partial

import wandb
import jax
from datasets import load_dataset
from transformers import AutoTokenizer

from data_utils import DataCollator, batchify, build_or_load_vocab, preprocess
from modeling_utils import Classifier
from training_utils import (
    Trainer,
    build_tx,
    cls_loss_fn,
    train_step,
    val_step,
)

IGNORE_IDX = -100

@dataclass
class TrainingArgs:
    base_model_id: str = "bert-base-uncased"
    logging_steps: int = 3000
    save_steps: int = 10500

    batch_size_per_device: int = 1
    max_epochs: int = 2
    seed: int = 42
    val_split: float = 0.05

    max_length: int = 4

    # tx_args
    lr: float = 3e-5
    init_lr: float = 0.0
    warmup_steps: int = 20000
    weight_decay: float = 0.0095

    base_dir: str = "training-expt"
    save_dir: str = "checkpoints"

    data_files: str = "../dataset/train-v2.csv"

    def __post_init__(self):
        os.makedirs(self.base_dir, exist_ok=True)
        self.save_dir = os.path.join(self.base_dir, self.save_dir)
        self.batch_size = self.batch_size_per_device * jax.device_count()


def main(args, logger):
    data = load_dataset("csv", data_files=[args.data_files], split="train")
    print("Samples: ", data[0], "\n", data[1])

    data = data.select(range(1010))

    browse_node_vocab = build_or_load_vocab(data, column_name="BROWSE_NODE_ID")
    brand_vocab = build_or_load_vocab(data, column_name="BRAND")
    print("VOCAB SIZE: ", len(browse_node_vocab), len(brand_vocab))

    tokenizer = AutoTokenizer.from_pretrained(args.base_model_id)

    data = data.map(lambda x: {"BRAND": IGNORE_IDX if x["BRAND"] is None else brand_vocab[x["BRAND"]]})
    data = preprocess(data, tokenizer.sep_token)
    data = data.train_test_split(args.val_split, seed=args.seed)
    print(data)
    tr_data, val_data = data["train"], data["test"]

    data_collator = DataCollator(tokenizer=tokenizer, max_length=args.max_length)

    model = Classifier.from_pretrained(
        args.base_model_id,
        num_browse_nodes=len(browse_node_vocab),
        num_brands=len(brand_vocab),
    )

    num_train_steps = (len(tr_data) // args.batch_size) * args.max_epochs
    tx, scheduler = build_tx(
        args.lr, args.init_lr, args.warmup_steps, num_train_steps, args.weight_decay
    )

    trainer = Trainer(
        args=args,
        data_collator=data_collator,
        batchify=batchify,
        train_step_fn=train_step,
        val_step_fn=val_step,
        loss_fn=partial(cls_loss_fn, ignore_idx=IGNORE_IDX),
        model_save_fn=model.save_pretrained,
        logger=logger,
        scheduler_fn=scheduler,
    )

    state = trainer.create_state(model, tx, num_train_steps, ckpt_dir=None)

    try:
        trainer.train(state, tr_data, val_data)
    except KeyboardInterrupt:
        print("Interrupting training from KEYBOARD")

    print("saving final model")
    model.save_pretrained("final-weights")


if __name__ == "__main__":
    args = TrainingArgs()

    print("##########################")
    print("DEVICES:", jax.devices())
    print("##########################")

    logger = wandb.init(project="amazon-ml-hack", config=asdict(args))
    args = replace(args, **dict(wandb.config))

    main(args, logger)
