import argparse
import collections
import datetime
import logging
import os
import pickle

import cv2
import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
import torch.utils.data
from pytorch_toolbelt import losses as L
from torch import nn
from torch.optim import lr_scheduler, AdamW, Adam
from torch.utils.data import DataLoader
from tqdm import tqdm

from .configs import *
# current project imports
from .datasets.spacenet_rgb import RGBADataset
from .datasets.transforms import TRANSFORMS
from .losses.bce_jaccard import BCEJaccardLoss
from .losses.dice import DiceLoss
from .losses.jaccard import JaccardLoss
from .utils.get_models import get_unet
from .utils.iou import binary_iou_numpy, binary_iou_pytorch
from .utils.logger import Logger
from .utils.radam import RAdam
from .utils.utils import load_model, set_seed, load_optim


def train_runner(model: nn.Module, model_name: str, results_dir: str, experiment: str = '', debug: bool = False, img_size: int = IMG_SIZE,
                 learning_rate: float = 1e-2, fold: int = 0, checkpoint: str = '',
                 epochs: int = 15, batch_size: int = 8, num_workers: int = 4, start_epoch: int = 0,
                 save_oof: bool = False, save_train_oof: bool = False, gpu_number: int = 1):
    """
    Model training runner

    Args: 
        model        : PyTorch model
        model_name   : string name for model for checkpoints saving
        results_dir  : directory to save results
        experiment   : string name for naming experiments
        debug        : if True, runs the debugging on few images 
        img_size     : size of images for training 
        learning_rate: initial learning rate (default = 1e-2) 
        fold         : training fold (default = 0)
        epochs       : number of the last epochs to train
        batch_size   : number of images in batch
        num_workers  : number of workers available
        from_epoch   : number of epoch to continue training   
        save_oof     : saves oof validation predictions. Default = False 
    """
    device = torch.device(f'cuda:{gpu_number}' if torch.cuda.is_available() else 'cpu')
    print(device)

    # load model weights to continue training    
    if checkpoint != '':
        model, ckpt = load_model(model, checkpoint) 
        moiu = ckpt['valid_miou']
        loss = ckpt['valid_loss']
        start_epoch = ckpt['epoch']+1
        print('Loaded model from {}, epoch {}'.format(checkpoint, epoch))
    
    model.to(device)

    # creates directories for checkpoints, tensorboard and predicitons
    checkpoints_dir = f'{results_dir}rgb/checkpoints/{model_name}{experiment}'
    predictions_dir = f'{results_dir}rgb/oof/{model_name}{experiment}'
    tensorboard_dir = f'{results_dir}rgb/tensorboard/{model_name}{experiment}'
    validations_dir = f'{results_dir}rgb/oof_val/{model_name}{experiment}'
    os.makedirs(checkpoints_dir, exist_ok=True)
    os.makedirs(predictions_dir, exist_ok=True)
    os.makedirs(tensorboard_dir, exist_ok=True)
    os.makedirs(validations_dir, exist_ok=True)
    logger = Logger(tensorboard_dir)
    print('\n', model_name, '\n')
    
    # datasets for train and validation
    df = pd.read_csv(f'{TRAIN_DIR}folds.csv')
    df_train = df[df.fold != fold]
    df_val = df[df.fold == fold]
    print(len(df_train.ImageId.values), len(df_val.ImageId.values))

    train_dataset = RGBADataset(
                images_dir = TRAIN_RGB,                 
                masks_dir = TRAIN_MASKS,
                labels_df = df_train, 
                img_size  = img_size,                 
                transforms= TRANSFORMS["medium"],
                normalise = True,           
                debug     = debug,  
    )    
    valid_dataset = RGBADataset(
                images_dir = TRAIN_RGB,                 
                masks_dir = TRAIN_MASKS,
                labels_df = df_val, 
                img_size  = img_size,                
                transforms= TRANSFORMS["d4"],
                normalise = True,           
                debug     = debug, 
    )            
    # dataloaders for train and validation
    dataloader_train = DataLoader(train_dataset,
                                  num_workers=num_workers,
                                  batch_size=batch_size,
                                  shuffle=True)                               

    dataloader_valid = DataLoader(valid_dataset,
                                  num_workers=num_workers,
                                  batch_size=batch_size,
                                  shuffle=False,
                                  drop_last=True)
    print('{} training images, {} validation images'.format(len(train_dataset), len(valid_dataset)))

    # optimizers and schedulers
    #optimizer = AdamW(model.parameters(), lr=learning_rate)
    optimizer = RAdam(model.parameters(), lr=learning_rate)    
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, mode='max', patience=5, verbose=True, factor=0.2, min_lr=1e-6)
    # load optimizer state continue training    
    #if checkpoint != '':
    #    optimizer = load_optim(optimizer, checkpoint, device)          

    # criteria
    criterion1 = nn.BCEWithLogitsLoss()                 
    criterion = BCEJaccardLoss(bce_weight=2, jaccard_weight=0.5, log_loss=False, log_sigmoid=True)
    #criterion = JaccardLoss(log_sigmoid=True, log_loss=False)
            
    # logging
    #if make_log:
    report_batch = 200  
    report_epoch = 20  
    log_file = os.path.join(checkpoints_dir, f'fold_{fold}.log')
    logging.basicConfig(filename=log_file, filemode="w", level=logging.DEBUG)  
    logging.info(f'Parameters:\n model_name: {model_name}\n, results_dir: {results_dir}\n, experiment: {experiment}\n, img_size: {img_size}\n, \
                 learning_rate: {learning_rate}\n, fold: {fold}\n, epochs: {epochs}\n, batch_size: {batch_size}\n, num_workers: {num_workers}\n, \
                 start_epoch: {start_epoch}\n, save_oof: {save_oof}\n, optimizer: {optimizer}\n, scheduler: {scheduler} \n, checkpoint: {start_epoch} \n')

    train_losses, val_losses = [], []
    best_val_loss = 1e+5
    best_val_metric = 0
    # training cycle
    print("Start training")
    for epoch in range(start_epoch, start_epoch + epochs + 1):
        print("Epoch", epoch)
        epoch_losses = []
        progress_bar = tqdm(dataloader_train, total=len(dataloader_train)) 
        progress_bar.set_description('Epoch {}'.format(epoch))       
        with torch.set_grad_enabled(True): # --> sometimes people write it, idk
            for batch_num, (img, target, _) in enumerate(progress_bar):
                img = img.to(device)
                target = target.float().to(device)
                prediction = model(img)                
                
                loss = criterion(prediction, target)
                optimizer.zero_grad()            
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 2)
                optimizer.step()
                epoch_losses.append(loss.detach().cpu().numpy())

                if batch_num and batch_num % report_batch == 0:                
                    logging.info(f'epoch: {epoch}; step: {batch_num}; loss: {np.mean(epoch_losses)} \n')
                
        # log loss history
        print("Epoch {}, Train Loss: {}".format(epoch, np.mean(epoch_losses)))
        train_losses.append(np.mean(epoch_losses))
        logger.scalar_summary('loss_train', np.mean(epoch_losses), epoch)
        logging.info(f'epoch: {epoch}; step: {batch_num}; loss: {np.mean(epoch_losses)} \n')

        # validate model
        val_loss = validate_loss(model, dataloader_valid, criterion1, epoch,
                                 validations_dir, device)

        valid_metrics = validate(model, dataloader_valid, criterion, epoch,
                                 validations_dir, save_oof, device)
        # logging metrics       
        logger.scalar_summary('loss_valid', valid_metrics['val_loss'], epoch)
        logger.scalar_summary('miou_valid', valid_metrics['miou'], epoch)
        valid_loss, val_metric = valid_metrics['val_loss'], valid_metrics['miou']
        logging.info(f'epoch: {epoch}; val_bce: {val_loss}; val_loss: {valid_loss}; val_miou: {val_metric}\n')
        val_losses.append(valid_metrics['val_loss'])
        
        # get current learning rate
        for param_group in optimizer.param_groups:            
            lr = param_group['lr']
        print(f'learning_rate: {lr}')    
        logging.info(f'learning_rate: {lr}\n')
        scheduler.step(val_metric)
        
        # save the best metric
        if valid_metrics['miou'] > best_val_metric:
            best_val_metric = valid_metrics['miou']
            # save model, optimizer and losses after every epoch
            print(f"Saving model with the best val metric {valid_metrics['miou']}, epoch {epoch}")
            checkpoint_filename = f"{model_name}_best_val_miou.pth"
            checkpoint_filepath = os.path.join(checkpoints_dir, checkpoint_filename)
            torch.save(
                {
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'epoch': epoch,
                    'loss': np.mean(epoch_losses),
                    'valid_loss': valid_metrics['val_loss'],
                    'valid_miou': valid_metrics['miou'],
                },
                checkpoint_filepath
            )
        # save the best loss
        if valid_metrics['val_loss'] < best_val_loss:
            best_val_loss = valid_metrics['val_loss']
            # save model, optimizer and losses after every epoch
            print(f"Saving model with the best val loss {valid_metrics['val_loss']}, epoch {epoch}")
            checkpoint_filename = "{}_best_val_loss.pth".format(model_name)
            checkpoint_filepath = os.path.join(checkpoints_dir, checkpoint_filename)
            torch.save(
                {
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'epoch': epoch,
                    'loss': np.mean(epoch_losses),
                    'valid_loss': valid_metrics['val_loss'],
                    'valid_miou': valid_metrics['miou'],
                },
                checkpoint_filepath
            )
        # save model, optimizer and losses after every n epoch
        elif epoch % report_epoch == 0:            
            print(f"Saving model at epoch {epoch}, val loss {valid_metrics['val_loss']}")
            checkpoint_filename = "{}_epoch_{}.pth".format(model_name, epoch)
            checkpoint_filepath = os.path.join(checkpoints_dir, checkpoint_filename)
            torch.save(
                {
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'epoch': epoch,
                    'loss': np.mean(epoch_losses),
                    'valid_loss': valid_metrics['val_loss'],
                    'valid_miou': valid_metrics['miou'],
                },
                checkpoint_filepath
            )    


def validate_loss(model: nn.Module, dataloader_valid: DataLoader, criterion: L, epoch: int,
                  predictions_dir: str, device: torch.device) -> float:
    """
    Validate model at the epoch end 
       
    Args: 
        model           : current model 
        dataloader_valid: dataloader for the validation fold 
        criterion       : loss criterion 
        epoch           : current epoch
        predictions_dir : directory for saving predictions

    Output:
        loss_valid: total validation loss, history 
    """
    with torch.no_grad():
        model.eval()
        val_losses = []
        progress_bar = tqdm(dataloader_valid, total=len(dataloader_valid))
        for img, target, _ in progress_bar:
            if torch.cuda.is_available():
                img = img.to(device)
                target = target.float().to(device)      
            output = model(img)          
            loss = criterion(output, target)
            val_losses.append(loss.detach().cpu().numpy())

    print("Epoch {}, Valid Loss: {}".format(epoch, np.mean(val_losses)))

    return np.mean(val_losses)


def validate(model: nn.Module, dataloader_valid: DataLoader, criterion: L, 
             epoch: int, predictions_dir: str, save_oof: bool, device: torch.device):
    """
    Validate model at the epoch end 
       
    Args: 
        model           : current model 
        dataloader_valid: dataloader for the validation fold 
        criterion       : loss criterion 
        epoch           : current epoch
        save_oof        : if true, calculate oof predictions and save them as png 
        predictions_dir : directory for saving predictions

    Output:
        metrics: dictionary with validation metrics 
    """
    with torch.no_grad():
        model.eval()      
        ious, val_losses = [], []        
        progress_bar = tqdm(dataloader_valid, total=len(dataloader_valid))
        
        for batch_num, (img, target, tile_ids) in enumerate(progress_bar):  # iterate over batches
            img = img.to(device)
            target = target.float().to(device)
            output = model(img) 
            loss = criterion(output, target)
            val_losses.append(loss.detach().cpu().numpy())         
              
            iou = binary_iou_pytorch(output, target, from_logits=True)            
            ious.append(iou.detach().cpu().numpy())
            # save predictions as pictures for the first batch
            if save_oof and batch_num == 0:
                output = torch.sigmoid(output)
                output = output.cpu().numpy().copy()
                for num, pred in enumerate(output, start=0):
                    tile_name = tile_ids[num]                     
                    if pred.ndim == 3:
                        pred = np.squeeze(pred, axis=0)
                    prob_mask = np.rint(pred*255).astype(np.uint8)                   
                    prob_mask_rgb = np.repeat(prob_mask[...,None], 3, 2) # repeat array for three channels    
                    cv2.imwrite(f"{predictions_dir}/{tile_name}.png", prob_mask_rgb)   

    # loss and metrics averaged over all batches
    print("Epoch {}, Valid Loss: {}, mIoU: {}".format(epoch, np.mean(val_losses), np.mean(ious)))    
    metrics = {'val_loss': np.mean(val_losses), 'miou': np.mean(ious)}
    
    return metrics


def main():
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    arg('--model-name', type=str, default='unet_resnet50', help='String model name used for saving')
    arg('--experiment', type=str, default='', help='String name for the experiment saving')
    arg('--encoder', type=str, default='resnet50', help='String model name used for saving')
    arg('--results-dir', type=str, default=RESULTS_DIR, help='Directory for saving model')
    arg('--checkpoint', type=str, default='', help='Filepath ro checkpoint')
    arg('--data-dir', type=str, default=TRAIN_DIR, help='Directory for saving model')
    arg('--image-size', type=int, default=IMG_SIZE, help='Image size for training')
    arg('--batch-size', type=int, default=4, help='Batch size during training')
    arg('--num-workers', type=int, default=2, help='Number of workers for dataloader. Default = 4.')
    arg('--epochs', type=int, default=3, help='Epoch to run')
    arg('--lr', type=float, default=1e-3, help='Initial learning rate')
    arg('--resume', type=bool, default=False, help='If True resumes training from the checkpoint')
    arg('--debug', type=bool, default=False, help='If True runs in debug mode')
    arg('--val-oof', type=bool, default=False)
    arg('--train-oof', type=bool, default=False)   
    arg('--gpu', type=int, default=1, help='Number of the GPU to use: 0, 1')
    args = parser.parse_args()
    print(args)
    set_seed(seed=1234)

    os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"]= str(args.gpu)
    # 1 channel, no activation (use sigmoid later)
    model = get_unet(encoder=args.encoder, in_channels = 4, num_classes = 1, activation = None) 

    # load model weights to continue training    
    if args.resume:
        checkpoints_dir = f'{args.results_dir}rgb/checkpoints/{args.model_name}'
        checkpoint_filename = f"{args.model_name}_best_val_miou.pth"        
        args.checkpoint = os.path.join(checkpoints_dir, checkpoint_filename)

        
    train_runner(
        model=model,
        model_name=args.model_name,
        results_dir=args.results_dir,
        experiment=args.experiment,
        checkpoint=args.checkpoint,
        debug=args.debug,
        img_size=args.image_size,
        learning_rate=args.lr,
        epochs=args.epochs,
        batch_size=args.batch_size,
        num_workers=args.num_workers,        
        save_oof=True,   
        gpu_number=1,              
    )


if __name__ == "__main__":
    main()
