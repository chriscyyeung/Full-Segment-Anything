# -*- coding: utf-8 -*-
"""
train the image encoder and mask decoder
freeze prompt image encoder
"""

# %% setup environment
import numpy as np
import matplotlib.pyplot as plt
import os

join = os.path.join
from tqdm import tqdm
from skimage import transform
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR
import monai
from monai.transforms import (
    Compose,
    Activations,
    AsDiscrete
)
# from segment_anything import sam_model_registry
from build_sam import sam_model_registry
import torch.nn.functional as F
import argparse
import random
from datetime import datetime
import shutil
import glob
from lr_scheduler import LinearWarmupWrapper

# set seeds
torch.manual_seed(2023)
torch.cuda.empty_cache()

# torch.distributed.init_process_group(backend="gloo")

os.environ["OMP_NUM_THREADS"] = "4"  # export OMP_NUM_THREADS=4
os.environ["OPENBLAS_NUM_THREADS"] = "4"  # export OPENBLAS_NUM_THREADS=4
os.environ["MKL_NUM_THREADS"] = "6"  # export MKL_NUM_THREADS=6
os.environ["VECLIB_MAXIMUM_THREADS"] = "4"  # export VECLIB_MAXIMUM_THREADS=4
os.environ["NUMEXPR_NUM_THREADS"] = "6"  # export NUMEXPR_NUM_THREADS=6


def show_mask(mask, ax, random_color=False):
    if random_color:
        color = np.concatenate([np.random.random(3), np.array([0.6])], axis=0)
    else:
        color = np.array([251 / 255, 252 / 255, 30 / 255, 0.6])
    h, w = mask.shape[-2:]
    mask_image = mask.reshape(h, w, 1) * color.reshape(1, 1, -1)
    ax.imshow(mask_image)


def show_box(box, ax):
    x0, y0 = box[0], box[1]
    w, h = box[2] - box[0], box[3] - box[1]
    ax.add_patch(
        plt.Rectangle((x0, y0), w, h, edgecolor="blue", facecolor=(0, 0, 0, 0), lw=2)
    )


class NpyDataset(Dataset):
    def __init__(self, data_root, bbox_shift=20):
        self.data_root = data_root
        # self.gt_path = join(data_root, "gts")
        # self.img_path = join(data_root, "imgs")
        # self.gt_path_files = sorted(
        #     glob.glob(join(self.gt_path, "**/*.npy"), recursive=True)
        # )
        # self.gt_path_files = [
        #     file
        #     for file in self.gt_path_files
        #     if os.path.isfile(join(self.img_path, os.path.basename(file)))
        # ]
        self.img_path_files = glob.glob(os.path.join(data_root, "**/imgs/**", "*.npy"), recursive=True)
        self.gt_path_files = glob.glob(os.path.join(data_root, "**/gts/**", "*.npy"), recursive=True)
        self.bbox_shift = bbox_shift
        print(f"number of images: {len(self.gt_path_files)}")

    def __len__(self):
        return len(self.gt_path_files)

    def __getitem__(self, index):
        # load npy image (1024, 1024, 3), [0,1]
        img_name = os.path.basename(self.gt_path_files[index])
        # img_1024 = np.load(
        #     join(self.img_path, img_name), "r", allow_pickle=True
        # )  # (1024, 1024, 3)
        # img_1024 = transform.resize(img_1024, (1024, 1024, 3))
        img = np.load(self.img_path_files[index], "r", allow_pickle=True)
        # convert the shape to (3, H, W)
        img_1024 = np.zeros((img.shape[0], img.shape[1], 3))
        img_1024[:, :, 0] = img[..., 0]
        img_1024[:, :, 1] = img[..., 0]
        img_1024[:, :, 2] = img[..., 0]
        img_1024 = np.transpose(img_1024, (2, 0, 1))
        assert (
            np.max(img_1024) <= 1.0 and np.min(img_1024) >= 0.0
        ), "image should be normalized to [0, 1]"
        gt = np.load(self.gt_path_files[index], "r", allow_pickle=True)  # multiple labels [0, 1,4,5...], (256,256)
        # assert img_name == os.path.basename(self.gt_path_files[index]), (
        #     "img gt name error" + self.gt_path_files[index] + self.npy_files[index]
        # )
        gt2D = np.squeeze(gt, axis=-1)
        # gt = transform.resize(gt, (1024, 1024), order=0)
        # label_ids = np.unique(gt)[1:]
        # gt2D = np.uint8(
        #     gt == random.choice(label_ids.tolist())
        # )  # only one label, (256, 256)
        assert np.max(gt2D) == 1 and np.min(gt2D) == 0.0, "ground truth should be 0, 1"
        y_indices, x_indices = np.where(gt2D > 0)
        x_min, x_max = np.min(x_indices), np.max(x_indices)
        y_min, y_max = np.min(y_indices), np.max(y_indices)
        # add perturbation to bounding box coordinates
        H, W = gt2D.shape
        x_min = max(0, x_min - random.randint(0, self.bbox_shift))
        x_max = min(W, x_max + random.randint(0, self.bbox_shift))
        y_min = max(0, y_min - random.randint(0, self.bbox_shift))
        y_max = min(H, y_max + random.randint(0, self.bbox_shift))
        bboxes = np.array([x_min, y_min, x_max, y_max])
        return (
            torch.tensor(img_1024).float(),
            torch.tensor(gt2D[None, :, :]).long(),
            torch.tensor(bboxes).float(),
            img_name,
        )

def dataset_sanity_check():
    # sanity test of dataset class
    tr_dataset = NpyDataset("c:/Users/chris/Data/Breast/WorkDirNpy/train")
    tr_dataloader = DataLoader(tr_dataset, batch_size=8, shuffle=True)
    for step, (image, gt, bboxes, names_temp) in enumerate(tr_dataloader):
        print(image.shape, gt.shape, bboxes.shape)
        # show the example
        _, axs = plt.subplots(1, 2, figsize=(25, 25))
        idx = random.randint(0, 7)
        axs[0].imshow(image[idx].cpu().permute(1, 2, 0).numpy())
        show_mask(gt[idx].cpu().numpy(), axs[0])
        show_box(bboxes[idx].numpy(), axs[0])
        axs[0].axis("off")
        # set title
        axs[0].set_title(names_temp[idx])
        idx = random.randint(0, 7)
        axs[1].imshow(image[idx].cpu().permute(1, 2, 0).numpy())
        show_mask(gt[idx].cpu().numpy(), axs[1])
        show_box(bboxes[idx].numpy(), axs[1])
        axs[1].axis("off")
        # set title
        axs[1].set_title(names_temp[idx])
        # plt.show()
        plt.subplots_adjust(wspace=0.01, hspace=0)
        plt.savefig("./data_sanitycheck.png", bbox_inches="tight", dpi=300)
        plt.close()
        break

# %% set up parser
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-i",
        "--tr_npy_path",
        type=str,
        default="data/npy/CT_Abd",
        help="path to training npy files; two subfolders: gts and imgs",
    )
    parser.add_argument(
        "-v",
        "--val_npy_path",
        type=str,
        help="path to validation npy files; two subfolders: gts and imgs"
    )
    parser.add_argument("-task_name", type=str, default="MedSAM-ViT-B")
    parser.add_argument("-model_type", type=str, default="vit_b")
    parser.add_argument(
        "-checkpoint", type=str, default="work_dir/SAM/sam_vit_b_01ec64.pth"
    )
    # parser.add_argument('-device', type=str, default='cuda:0')
    parser.add_argument(
        "--load_pretrain", type=bool, default=True, help="use wandb to monitor training"
    )
    parser.add_argument("-pretrain_model_path", type=str, default="")
    parser.add_argument("-work_dir", type=str, default="./work_dir")
    # train
    parser.add_argument("-num_epochs", type=int, default=1000)
    parser.add_argument("-batch_size", type=int, default=2)
    parser.add_argument("-num_workers", type=int, default=0)
    # Optimizer parameters
    parser.add_argument(
        "-weight_decay", type=float, default=0.01, help="weight decay (default: 0.01)"
    )
    parser.add_argument(
        "-lr", type=float, default=0.0001, metavar="LR", help="learning rate (absolute lr)"
    )
    parser.add_argument(
        "-use_wandb", type=bool, default=False, help="use wandb to monitor training"
    )
    parser.add_argument("-use_amp", action="store_true", default=False, help="use amp")
    parser.add_argument(
        "--resume", type=str, default="", help="Resuming training from checkpoint"
    )
    parser.add_argument("--device", type=str, default="cuda:0")
    return parser.parse_args()


class MedSAM(nn.Module):
    def __init__(
        self,
        image_encoder,
        mask_decoder,
        prompt_encoder,
    ):
        super().__init__()
        self.image_encoder = image_encoder
        self.mask_decoder = mask_decoder
        self.prompt_encoder = prompt_encoder
        # freeze prompt encoder
        for param in self.prompt_encoder.parameters():
            param.requires_grad = False

    def forward(self, image, box):
        image_embedding = self.image_encoder(image)  # (B, 256, 64, 64)
        # do not compute gradients for prompt encoder
        with torch.no_grad():
            box_torch = torch.as_tensor(box, dtype=torch.float32, device=image.device)
            if len(box_torch.shape) == 2:
                box_torch = box_torch[:, None, :]  # (B, 1, 4)

            sparse_embeddings, dense_embeddings = self.prompt_encoder(
                points=None,
                boxes=None,
                masks=None,
            )
        low_res_masks, _ = self.mask_decoder(
            image_embeddings=image_embedding,  # (B, 256, 64, 64)
            image_pe=self.prompt_encoder.get_dense_pe(),  # (1, 256, 64, 64)
            sparse_prompt_embeddings=sparse_embeddings,  # (B, 2, 256)
            dense_prompt_embeddings=dense_embeddings,  # (B, 256, 64, 64)
            multimask_output=False,
        )
        ori_res_masks = F.interpolate(
            low_res_masks,
            size=(image.shape[2], image.shape[3]),
            mode="bilinear",
            align_corners=False,
        )
        return ori_res_masks


def main(args):
    if args.use_wandb:
        import wandb

        wandb.login()
        run = wandb.init(
            project=args.task_name,
            config={
                "lr": args.lr,
                "batch_size": args.batch_size,
                "data_path": args.tr_npy_path,
                "model_type": args.model_type,
            },
        )

    # set up model for training
    # device = args.device
    run_id = datetime.now().strftime("%Y%m%d-%H%M")
    model_save_path = join(args.work_dir, args.task_name + "-" + run_id)
    device = torch.device(args.device)
    # set up model
    os.makedirs(model_save_path, exist_ok=True)
    shutil.copyfile(
        __file__, join(model_save_path, run_id + "_" + os.path.basename(__file__))
    )

    sam_model = sam_model_registry[args.model_type](checkpoint=args.checkpoint)
    medsam_model = MedSAM(
        image_encoder=sam_model.image_encoder,
        mask_decoder=sam_model.mask_decoder,
        prompt_encoder=sam_model.prompt_encoder,
    ).to(device)
    medsam_model.train()

    print(
        "Number of total parameters: ",
        sum(p.numel() for p in medsam_model.parameters()),
    )  # 93735472
    print(
        "Number of trainable parameters: ",
        sum(p.numel() for p in medsam_model.parameters() if p.requires_grad),
    )  # 93729252

    img_mask_encdec_params = list(medsam_model.image_encoder.parameters()) + list(
        medsam_model.mask_decoder.parameters()
    )
    optimizer = torch.optim.AdamW(
        img_mask_encdec_params, lr=args.lr, weight_decay=args.weight_decay
    )
    print(
        "Number of image encoder and mask decoder parameters: ",
        sum(p.numel() for p in img_mask_encdec_params if p.requires_grad),
    )  # 93729252
    seg_loss = monai.losses.DiceLoss(sigmoid=True, squared_pred=True, reduction="mean")
    # cross entropy loss
    # ce_loss = nn.BCEWithLogitsLoss(reduction="mean")
    ce_loss = monai.losses.FocalLoss(reduction="mean")
    # %% train
    num_epochs = args.num_epochs
    iter_num = 0
    losses = []
    best_loss = 1e10
    train_dataset = NpyDataset(args.tr_npy_path)
    val_dataset = NpyDataset(args.val_npy_path)

    print("Number of training samples: ", len(train_dataset))
    train_dataloader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=True,
    )
    print("Number of validation samples: ", len(val_dataset))
    val_dataloader = DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        pin_memory=True,
    )

    start_epoch = 0
    if args.resume is not None:
        if os.path.isfile(args.resume):
            ## Map model to be loaded to specified single GPU
            checkpoint = torch.load(args.resume, map_location=device)
            start_epoch = checkpoint["epoch"] + 1
            medsam_model.load_state_dict(checkpoint["model"])
            optimizer.load_state_dict(checkpoint["optimizer"])
    if args.use_amp:
        scaler = torch.cuda.amp.GradScaler()

    start_step = start_epoch * len(train_dataloader)
    max_steps = len(train_dataloader) * num_epochs
    warmup_steps = 1000
    last_cosine_step = start_step - warmup_steps if start_step > warmup_steps else 0
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, 
        max_steps - warmup_steps, 
        last_epoch=last_cosine_step - 1
    )
    scheduler = LinearWarmupWrapper(
        optimizer, 
        lr_scheduler, 
        args.lr, 
        warmup_steps=warmup_steps, 
        last_step=start_step - 1
    )

    dice_metric = monai.metrics.DiceMetric(reduction="mean")
    iou_metric = monai.metrics.MeanIoU(reduction="mean")
    hd95_metric = monai.metrics.HausdorffDistanceMetric(percentile=95.0, reduction="mean")
    confusion_matrix_metric = monai.metrics.ConfusionMatrixMetric( 
        metric_name=["accuracy", "precision", "sensitivity", "specificity", "f1_score"],
        reduction="mean"
    )
    post_pred = Compose([Activations(sigmoid=True), AsDiscrete(threshold=0.5)])

    for epoch in range(start_epoch, num_epochs):
        medsam_model.train()
        epoch_loss = 0
        for step, (image, gt2D, boxes, _) in enumerate(tqdm(train_dataloader)):
            optimizer.zero_grad()
            boxes_np = boxes.detach().cpu().numpy()
            image, gt2D = image.to(device), gt2D.to(device)
            if args.use_amp:
                ## AMP
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    medsam_pred = medsam_model(image, boxes_np)
                    loss = seg_loss(medsam_pred, gt2D) + ce_loss(
                        medsam_pred, gt2D.float()
                    )
                scaler.scale(loss).backward()
                scheduler.step()
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
            else:
                medsam_pred = medsam_model(image, boxes_np)
                loss = seg_loss(medsam_pred, gt2D) + ce_loss(medsam_pred, gt2D.float())
                loss.backward()
                scheduler.step()
                optimizer.step()
                optimizer.zero_grad()

            epoch_loss += loss.item()
            iter_num += 1
        
        epoch_loss /= step
        losses.append(epoch_loss)
        print(f'Time: {datetime.now().strftime("%Y%m%d-%H%M")}, Epoch: {epoch}, Loss: {epoch_loss}')

        medsam_model.eval()
        val_loss = 0
        with torch.no_grad():
            for step, (image, gt2D, boxes, _) in enumerate(tqdm(val_dataloader)):
                boxes_np = boxes.detach().cpu().numpy()
                image, gt2D = image.to(device), gt2D.to(device)
                medsam_pred = medsam_model(image, boxes_np)
                loss = seg_loss(medsam_pred, gt2D) + ce_loss(medsam_pred, gt2D.float())
                val_loss += loss.item()

                val_outputs = [post_pred(i) for i in monai.data.utils.decollate_batch(medsam_pred)]
                val_labels = monai.data.utils.decollate_batch(gt2D)

                dice_metric(y_pred=val_outputs, y=val_labels)
                iou_metric(y_pred=val_outputs, y=val_labels)
                hd95_metric(y_pred=val_outputs, y=val_labels)
                confusion_matrix_metric(y_pred=val_outputs, y=val_labels)
        val_loss /= step
        print(f'Time: {datetime.now().strftime("%Y%m%d-%H%M")}, Epoch: {epoch}, Val loss: {val_loss}')

        dice = dice_metric.aggregate().item()
        iou = iou_metric.aggregate().item()
        hd95 = hd95_metric.aggregate().item()
        cm = confusion_matrix_metric.aggregate()

        for param_group in optimizer.param_groups:
            current_lr = param_group["lr"]

        # Log a random sample of test images along with their ground truth and predictions
        random.seed(2024)
        sample = random.sample(range(len(val_dataset)), 5)

        inputs = torch.stack([val_dataset[i][0] for i in sample])
        labels = torch.stack([val_dataset[i][1] for i in sample])
        boxes = torch.stack([val_dataset[i][2] for i in sample])
        with torch.no_grad():
            outputs = medsam_model(inputs.to(device=device), boxes.detach().cpu().numpy())

        fig, axes = plt.subplots(5, 3, figsize=(9, 15))
        for i in range(5):
            axes[i, 0].imshow(inputs[i, 0, ...].squeeze(), cmap="gray")
            axes[i, 1].imshow(labels[i, 0, ...].squeeze(), cmap="gray")
            im = axes[i, 2].imshow(torch.sigmoid(outputs[i]).squeeze().detach().cpu(), cmap="gray")
            
            # Create an additional axis for the colorbar
            cax = fig.add_axes([axes[i, 2].get_position().x1 + 0.01,
                                axes[i, 2].get_position().y0,
                                0.02,
                                axes[i, 2].get_position().height])
            fig.colorbar(im, cax=cax)

        if args.use_wandb:
            run.log({
                "epoch_loss": epoch_loss, 
                "val_loss": val_loss,
                "dice": dice,
                "iou": iou,
                "95hd": hd95,
                "accuracy": cm[0].item(),
                "precision": cm[1].item(),
                "sensitivity": cm[2].item(),
                "specificity": cm[3].item(),
                "f1_score": cm[4].item(),
                "lr": current_lr,
                "examples": wandb.Image(fig)}
            )
        
        dice_metric.reset()
        iou_metric.reset()
        hd95_metric.reset()
        confusion_matrix_metric.reset()

        ## save the latest model
        checkpoint = {
            "model": medsam_model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "epoch": epoch,
        }
        torch.save(checkpoint, join(model_save_path, "medsam_model_latest.pth"))
        ## save the best model
        if val_loss < best_loss:
            best_loss = val_loss
            checkpoint = {
                "model": medsam_model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "epoch": epoch,
            }
            torch.save(checkpoint, join(model_save_path, "medsam_model_best.pth"))

        # %% plot loss
        plt.plot(losses)
        plt.title("Dice + Cross Entropy Loss")
        plt.xlabel("Epoch")
        plt.ylabel("Loss")
        plt.savefig(join(model_save_path, args.task_name + "train_loss.png"))
        plt.close()
    
    run.finish()


if __name__ == "__main__":
    args = parse_args()
    main(args)
