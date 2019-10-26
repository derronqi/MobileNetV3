# -*- coding: UTF-8 -*-

'''
Train the model
Ref: https://pytorch.org/tutorials/beginner/transfer_learning_tutorial.html
'''

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.autograd import Variable
import time
import os
from mobileNetV3 import MobileNetV3
import argparse
import copy
from math import cos, pi

from statistics import *
from EMA import EMA
from LabelSmoothing import LabelSmoothingLoss
from DataLoader import dataloaders
from ResultWriter import ResultWriter
from CosineWarmupLR import CosineWarmupLR

def train(args, model, dataloader, criterion, optimizer, scheduler, use_gpu, epoch, ema=None, save_file_name='train.csv'):
    '''
    train the model
    '''
    # save result every epoch
    resultWriter = ResultWriter(args.save_path, save_file_name)
    if epoch == 0:
        resultWriter.create_csv(['epoch', 'loss', 'top-1', 'top-5'])

    # use gpu or not
    device = torch.device('cuda' if use_gpu else 'cpu')

    # statistical information
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(
        len(dataloader),
        [batch_time, data_time, losses, top1, top5],
        prefix="Epoch: [{}]".format(epoch))
    
    # update lr here if using stepLR
    if args.lr_decay == 'step':
        scheduler.step(epoch)
    
    # Set model to training mode
    model.train()

    end = time.time()

    # Iterate over data
    for i, (inputs, labels) in enumerate(dataloader):
        # measure data loading time
        data_time.update(time.time() - end)

        inputs = inputs.to(device)
        labels = labels.to(device)
        
        # zero the parameter gradients
        optimizer.zero_grad()

        # forward
        # track history
        with torch.set_grad_enabled(True):
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            
            # measure accuracy and record loss
            acc1, acc5 = accuracy(outputs, labels, topk=(1, 5))
            losses.update(loss.item(), inputs.size(0))
            top1.update(acc1[0], inputs.size(0))
            top5.update(acc5[0], inputs.size(0))
            
            # backward + optimize
            loss.backward()
            if args.lr_decay == 'cos':
                # update lr here if using cosine lr decay
                scheduler.step(epoch * len(dataloader) + i)
            optimizer.step()
            if args.ema_decay > 0:
                # EMA update after training(every iteration)
                ema.update()
                
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)
            
    # write training result to file
    resultWriter.write_csv([epoch, losses.avg, top1.avg.item(), top5.avg.item()])
            
    print()
    # there is a bug in get_lr() if using pytorch 1.1.0, see https://github.com/pytorch/pytorch/issues/22107
    # so here we don't use get_lr()
    # print('lr:%.6f' % scheduler.get_lr()[0])
    print('lr:%.6f' % scheduler.optimizer.param_groups[0]['lr'])
    print('Train ***    Loss:{losses.avg:.2e}    Acc@1:{top1.avg:.2f}    Acc@5:{top5.avg:.2f}'.format(losses=losses, top1=top1, top5=top5))

    if epoch % args.save_epoch_freq == 0 and epoch != 0:
        if not os.path.exists(args.save_path):
            os.makedirs(args.save_path)
        torch.save(model.state_dict(), os.path.join(args.save_path, "epoch_" + str(epoch) + ".pth"))

def validate(args, model, dataloader, criterion, use_gpu, epoch, ema=None, save_file_name='val.csv'):
    '''
    validate the model
    '''

    # save result every epoch
    resultWriter = ResultWriter(args.save_path, save_file_name)
    if epoch == 0:
        resultWriter.create_csv(['epoch', 'loss', 'top-1', 'top-5'])

    device = torch.device('cuda' if use_gpu else 'cpu')

    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(
        len(dataloader),
        [batch_time, data_time, losses, top1, top5],
        prefix="Epoch: [{}]".format(epoch))
    if args.ema_decay > 0:
        # apply EMA at validation stage
        ema.apply_shadow()
    # Set model to evaluate mode
    model.eval()

    end = time.time()

    # Iterate over data
    for i, (inputs, labels) in enumerate(dataloader):
        # measure data loading time
        data_time.update(time.time() - end)
        
        inputs = inputs.to(device)
        labels = labels.to(device)

        with torch.set_grad_enabled(False):
            
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            
            # measure accuracy and record loss
            acc1, acc5 = accuracy(outputs, labels, topk=(1, 5))
            losses.update(loss.item(), inputs.size(0))
            top1.update(acc1[0], inputs.size(0))
            top5.update(acc5[0], inputs.size(0))
            batch_time.update(time.time() - end)
            end = time.time()
            

    if args.ema_decay > 0:
        # restore the origin parameters after val
        ema.restore()
    # write val result to file
    resultWriter.write_csv([epoch, losses.avg, top1.avg.item(), top5.avg.item()])

    print(' Val  ***    Loss:{losses.avg:.2e}    Acc@1:{top1.avg:.2f}    Acc@5:{top5.avg:.2f}'.format(losses=losses, top1=top1, top5=top5))     
    print()

    if epoch % args.save_epoch_freq == 0 and epoch != 0:
        if not os.path.exists(args.save_path):
            os.makedirs(args.save_path)
        torch.save(model.state_dict(), os.path.join(args.save_path, "epoch_" + str(epoch) + ".pth"))

    top1_acc = top1.avg.item()
    top5_acc = top5.avg.item()
    
    return top1_acc, top5_acc

def train_model(args, model, dataloader, criterion, optimizer, scheduler, use_gpu):
    '''
    train the model
    '''
    since = time.time()

    ema = None
    # exponential moving average
    if args.ema_decay > 0:
        ema = EMA(model, decay=args.ema_decay)
        ema.register()

    best_model_wts = copy.deepcopy(model.state_dict())
    best_acc = 0.0
    correspond_top5 = 0.0

    for epoch in range(args.start_epoch, args.num_epochs):

        train(args, model, dataloader['train'], criterion, optimizer, scheduler, use_gpu, epoch, ema)
        top1_acc, top5_acc = validate(args, model, dataloader['val'], criterion, use_gpu, epoch, ema)

        # deep copy the model if it has higher top-1 accuracy
        if top1_acc > best_acc:
            best_acc = top1_acc
            correspond_top5 = top5_acc
            if args.ema_decay > 0:
                ema.apply_shadow()
            best_model_wts = copy.deepcopy(model.state_dict())
            if args.ema_decay > 0:
                ema.restore()

    print(args.save_path)
    print('Best val top-1 Accuracy: {:4f}'.format(best_acc))
    print('Corresponding top-5 Accuracy: {:4f}'.format(correspond_top5))
    
    time_elapsed = time.time() - since
    print('Training complete in {:.0f}h {:.0f}m {:.0f}s'.format(time_elapsed // 3600, (time_elapsed % 3600) // 60, time_elapsed % 60))

    # load best model weights
    model.load_state_dict(best_model_wts)
    # save best model weights
    torch.save(model.state_dict(), os.path.join(args.save_path, 'best_model_wts-' + '{:.2f}'.format(best_acc) + '.pth'))
    return model

if __name__ == '__main__':

    import warnings
    warnings.filterwarnings('ignore')

    parser = argparse.ArgumentParser(description='PyTorch implementation of MobileNetV3')
    # Root catalog of images
    parser.add_argument('--data-dir', type=str, default='/media/data2/chenjiarong/ImageData')
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--num-epochs', type=int, default=150)
    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--num-workers', type=int, default=4)
    #parser.add_argument('--gpus', type=str, default='0')
    parser.add_argument('--print-freq', type=int, default=1000)
    parser.add_argument('--save-epoch-freq', type=int, default=1)
    parser.add_argument('--save-path', type=str, default='/media/data2/chenjiarong/saved-model')
    parser.add_argument('--resume', type=str, default='', help='For training from one checkpoint')
    parser.add_argument('--start-epoch', type=int, default=0, help='Corresponding to the epoch of resume')
    parser.add_argument('--ema-decay', type=float, default=0.9999, help='The decay of exponential moving average ')
    parser.add_argument('--dataset', type=str, default='ImageNet', help='The dataset to be trained')
    parser.add_argument('--mode', type=str, default='large', help='large or small MobileNetV3')
    # parser.add_argument('--num-class', type=int, default=1000)
    parser.add_argument('--width-multiplier', type=float, default=1.0, help='width multiplier')
    parser.add_argument('--dropout', type=float, default=0.2, help='dropout rate')
    parser.add_argument('--label-smoothing', type=float, default=0.1, help='label smoothing')
    parser.add_argument('--lr-decay', type=str, default='step', help='learning rate decay method, step or cos')
    parser.add_argument('--step-size', type=int, default=3, help='step size in stepLR()')
    parser.add_argument('--gamma', type=float, default=0.99, help='gamma in stepLR()')
    parser.add_argument('--lr-min', type=float, default=0, help='minium lr using in CosineWarmupLR')
    parser.add_argument('--warmup-epochs', type=int, default=0, help='warmup epochs using in CosineWarmupLR')
    parser.add_argument('--optimizer', type=str, default='sgd', help='optimizer')
    parser.add_argument('--weight-decay', type=float, default=1e-5, help='weight decay')
    args = parser.parse_args()

    args.lr_decay = args.lr_decay.lower()
    args.dataset = args.dataset.lower()
    args.optimizer = args.optimizer.lower()

    # folder to save what we need in this type: MobileNetV3-mode-dataset-width_multiplier-dropout-lr-batch_size-ema_decay-label_smoothing
    folder_name = ['MobileNetV3', args.mode, args.dataset, 'wm'+str(args.width_multiplier), 'dp'+str(args.dropout), 'lr'+str(args.lr), 'bs'+str(args.batch_size), 'ed'+str(args.ema_decay), 'ls'+str(args.label_smoothing), args.optimizer+str(args.weight_decay)]
    if args.lr_decay == 'step':
        folder_name.append(args.lr_decay+str(args.step_size)+'&'+str(args.gamma))
    elif args.lr_decay == 'cos':
        folder_name.append(args.lr_decay+str(args.warmup_epochs) + '&' + str(args.lr_min))
    folder_name = '-'.join(folder_name)
    args.save_path = os.path.join(args.save_path, folder_name)
    if not os.path.exists(args.save_path):
        os.makedirs(args.save_path)

    # read data
    dataloaders = dataloaders(args)

    # different input size and number of classes for different datasets
    if args.dataset == 'imagenet':
        input_size = 224
        num_class = 1000
    if args.dataset == 'cifar100':
        input_size = 32
        num_class = 100
    elif args.dataset == 'cifar10':
        input_size = 32
        num_class = 10
        
    # use gpu or not
    use_gpu = torch.cuda.is_available()
    print("use_gpu:{}".format(use_gpu))

    # get model
    model = MobileNetV3(mode=args.mode, classes_num=num_class, input_size=input_size, width_multiplier=args.width_multiplier, dropout=args.dropout)

    if use_gpu:
        if torch.cuda.device_count() > 1:
            model = torch.nn.DataParallel(model)
        model.to(torch.device('cuda'))
    else:
        model.to(torch.device('cpu'))

    if args.resume:
        if os.path.isfile(args.resume):
            print(("=> loading checkpoint '{}'".format(args.resume)))
            model.load_state_dict(torch.load(args.resume))
        else:
            print(("=> no checkpoint found at '{}'".format(args.resume)))

    if args.label_smoothing > 0:
        # using Label Smoothing
        criterion = LabelSmoothingLoss(num_class, label_smoothing=args.label_smoothing)
    else:
        criterion = nn.CrossEntropyLoss()
    
    if args.optimizer == 'sgd':
        optimizer_ft = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=args.weight_decay)
    elif args.optimizer == 'rmsprop':
        optimizer_ft = optim.RMSprop(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=args.weight_decay)

    if args.lr_decay == 'step':
        # Decay LR by a factor of 0.99 every 3 epoch
        lr_scheduler = lr_scheduler.StepLR(optimizer_ft, step_size=args.step_size, gamma=args.gamma)
    elif args.lr_decay == 'cos':
        lr_scheduler = CosineWarmupLR(optimizer=optimizer_ft, epochs=args.num_epochs, iter_in_one_epoch=len(dataloaders['train']), lr_min=args.lr_min, warmup_epochs=args.warmup_epochs)

    model = train_model(args=args,
                        model=model,
                        dataloader=dataloaders,
                        criterion=criterion,
                        optimizer=optimizer_ft,
                        scheduler=lr_scheduler,
                        use_gpu=use_gpu)