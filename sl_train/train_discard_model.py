import os
import argparse
import sys
sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

from model.models import DiscardModel
from dataset.data import TenhouDataset, TenhouIterableDataset, process_data, collate_fn_discard

import torch
from torch.optim import Adam
from torch.utils.data import DataLoader
from torch.nn import CrossEntropyLoss
import wandb
import tqdm


def make_loader(dataset: TenhouIterableDataset, collate_fn):
    return DataLoader(
        dataset,
        batch_size=512,
        num_workers=4,
        collate_fn=collate_fn
    )

@torch.no_grad()
def model_test(model, test_loader, fast=False):
    acc = 0
    total = 0
    steps, step = 256 if fast else -1, 0
    for features, labels in (pbar := tqdm.tqdm(test_loader)):
        features, labels = features.to(device), labels.to(device)
        output = model(features).softmax(1)
        available = features[:, :4].sum(1) != 0
        # available = (features[:, :16] * features[:, 86: 90].repeat_interleave(4, 1)).sum(1) != 0
        pred = (output * available).argmax(1)
        correct = (pred == labels).sum()
        acc += correct
        total += len(labels)
        step += 1
        if step % 100 == 0:
            pbar.set_postfix_str("acc {:.2f}".format(acc/total))
        if step == steps:
            break
    return acc / total


mode = 'discard'
parser = argparse.ArgumentParser()
parser.add_argument('--num_layers', '-n', default=50, type=int)
parser.add_argument('--epochs', '-e', default=10, type=int)
args = parser.parse_args()

experiment = wandb.init(project='Mahjong', resume='allow', anonymous='must', name=f'train-{mode}-sl')
train_set = TenhouDataset(data_dir='data', batch_size=128, mode=mode, target_length=2)
test_set = TenhouDataset(data_dir='data', batch_size=128, mode=mode, target_length=2)
length = len(train_set)
len_train = int(0.8 * length)
train_set.data_files, test_set.data_files = train_set.data_files[:len_train], train_set.data_files[len_train:]

num_layers = args.num_layers
in_channels = 291
model = DiscardModel(num_layers=num_layers, in_channels=in_channels)
model = torch.compile(model)  # speed up training by ~13% in default setup
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
model.to(device)
optim = Adam(model.parameters())
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optim, mode='max', patience=1)
loss_fcn = CrossEntropyLoss()
epochs = args.epochs

os.makedirs(f'output/{mode}-model/checkpoints', exist_ok=True)
max_acc = 0
global_step = 0
# patched dataset and loader
dataset, test_dataset = [
    TenhouIterableDataset(
        data_dir='data',
        exclude_files=exclusions,  # exclude testing set
        mode='discard',
        target_length=2,
        shuffle=True
    )
    for exclusions in [set(test_set.data_files), set(train_set.data_files)]
]
train_loader = make_loader(dataset, collate_fn_discard)
test_loader = make_loader(test_dataset, collate_fn_discard)

for epoch in range(epochs):
    for features, labels in (pbar := tqdm.tqdm(train_loader)):
        features, labels = features.to(device, non_blocking=True), labels.to(device, non_blocking=True)
        output = model(features)
        loss = loss_fcn(output, labels)
        optim.zero_grad()
        loss.backward()
        optim.step()
        global_step += 1
        if global_step % 1000 == 0:
            pbar.set_postfix_str(f"epoch={epoch+1} loss={loss.item():.3f} max_acc={max_acc:.3f}")
        experiment.log({
            'train loss': loss.item(),
            'epoch': epoch + 1
        })

    train_set.reset()

    torch.save({"state_dict": model.state_dict(), "num_layers": num_layers, "in_channels": in_channels}, f'output/{mode}-model/checkpoints/epoch_{epoch + 1}.pt')
    model.eval()
    acc = model_test(model, test_loader)
    if acc > max_acc:
        max_acc = acc
        torch.save({"state_dict": model.state_dict(), "num_layers": num_layers, "in_channels": in_channels}, f'output/{mode}-model/checkpoints/best.pt')
    model.train()

    experiment.log({
        'epoch': epoch + 1,
        'test_acc': acc,
        'lr': optim.param_groups[0]['lr']
    })
    scheduler.step(acc)

