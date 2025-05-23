# Code for "ActionCLIP: ActionCLIP: A New Paradigm for Action Recognition"
# arXiv:
# Mengmeng Wang, Jiazheng Xing, Yong Liu

import os
import torch.nn as nn
from datasets import Action_DATASETS, Action_DATASETS_orig
from torch.utils.data import DataLoader
from tqdm import tqdm
import wandb
import argparse
import shutil
from pathlib import Path
import yaml
from dotmap import DotMap
import pprint
from modules.Visual_Prompt import visual_prompt
from utils.KLLoss import KLLoss
from test import validate, calculate_similarity
from utils.Augmentation import *
from utils.solver import _optimizer, _lr_scheduler
from utils.tools import *
from utils.Text_Prompt import *
from utils.saving import  *
import sys
sys.path.insert(0, "../explainable_bounding_box/ml-no-token-left-behind/external/tamingtransformers/")
sys.path.append("./../explainable_bounding_box/ml-no-token-left-behind/external/TransformerMMExplainability/")
from prompt import PromptLoss, TextCLIP, ImageCLIP
from prompt import PromptLoss2 as pl2
from helpers import *
import random
# from TSSTANET.tsstanet import tanet, sanet, stanet, stanet_af

import torch

def create_prompt_loss_dict(label_names, perceptor, replace_grad, device):
    # keys = sorted(list(label_names.keys()))
    res = {}
    for label_num, label_name in label_names:
        prompt = f'Video of a teacher {" ".join(label_name.split("_"))}'
        res[label_num] = PromptLoss(prompt, perceptor, replace_grad).to(device)
    return res
    
def bounding_box_loss(criterion_list, b, t, c, h, w, list_id, aug_masks, lambdas, texts, promptCrit, fusion_model):
    lossAll = []
    # per_frame = False
    loop_list = True
    text_list = []
    image_list = []
    if len(criterion_list) > 0:
        # for each label in the list and for each invidividual video (there are 16 of them)
        if loop_list:
            videos = videos.reshape(b,t,c,h,w)
            for idx, label in enumerate(list_id.detach().cpu()):
                # find the loss for this specific bounding box
                prompt_idx = int(label)
                crit = criterion_list[prompt_idx]
                curr_image = videos[idx]
                curr_mask = aug_masks[idx]
                curr_lambda = lambdas[idx]
                token = texts[idx]
    
                res, text_embedding, image_embedding = promptCrit(curr_image, curr_mask, curr_lambda, token)
                image_list.append(image_embedding)
    
                text_list.append(text_embedding[0])
                lossAll.append(res)
            loss_all = (sum(lossAll)/len(lossAll))
            text_embedding = torch.stack(text_list)
            image_embedding = torch.stack(image_list)
            image_embedding = image_embedding.view(b,t,-1)
            image_embedding = fusion_model(image_embedding)
            # print(sum(lossAll), len(lossAll))
        # else:
        #     # give the loss function the current text to unify the two
        #     videos = videos.reshape(b,t,c,h,w)
        #     print(videos.shape, aug_masks.shape, lambdas.shape, texts.shape)
        #     loss_all = promptCrit(videos, aug_masks, lambdas, texts)
        #     raise ValueError("test")
    return loss_all, image_embedding, text_embedding


def train_classifier(start_epoch, 
                     loss_img,
                     loss_txt,
                     criterion_list,
                     promptCrit,
                     lr_scheduler,
                     config, 
                     text_dict,
                     model_image, 
                     model_text, 
                     fusion_model, 
                     train_loader, 
                     val_loader, 
                     optimizer, 
                     num_text_aug,
                     classes,
                     perceptor,
                     working_dir,
                     device,
                     lambda_bb,
                     lambda_ce):
    best_prec1 = -1
    cross_entropy = nn.CrossEntropyLoss()
    clamp_with_grad = ClampWithGrad.apply

    ##################################################
    # e_dim = perceptor.quantize.e_dim
    # n_toks = perceptor.quantize.n_e
    # vqgan_weights = perceptor.quantize.embedding.weight
    # z_min = vqgan_weights.min(dim=0).values[None, :, None, None]
    # z_max = vqgan_weights.max(dim=0).values[None, :, None, None]
    
    # one_hot = F.one_hot(torch.randint(n_toks, [1 * 1], device=device), n_toks).float()
    # z = one_hot @ vqgan_weights
    # z = z.view([-1, toksY, toksX, e_dim]).permute(0, 3, 1, 2)
    # z = torch.rand_like(z)*2
    # z_orig = z.clone()
    # z.requires_grad_(True)
    ##################################################

    ################### Train Classifier ####################################
    # scaler = torch.cuda.amp.GradScaler()
    for epoch in range(start_epoch, config.solver.epochs):
        model_image.train()
        model_text.train()
        fusion_model.train()
        
        ###########################################################################
        # x = rand_vqgan_weights.movedim(1, 3)
        # codebook = vqgan_model.quantize.embedding.weight
        
        # # vector_quantize
        # d = x.pow(2).sum(dim=-1, keepdim=True) + codebook.pow(2).sum(dim=1) - 2 * x @ codebook.T
        # indices = d.argmin(-1)
        # x_q = F.one_hot(indices, codebook.shape[0]).to(d.dtype) @ codebook
        # rand_vqgan_weights_q = replace_grad(x_q, x).movedim(3, 1)

        # # clamp the gradient between the minimum and maximum weights of the original
        # out = clamp_with_grad(vqgan_model.decode(rand_vqgan_weights_q).add(1).div(2), 0, 1)
        ###########################################################################

        running_loss_all = 0
        running_kl = 0
        running_ce = 0
        running_total = 0

        for kkk,data in enumerate(tqdm(train_loader)):
            if config.solver.type != 'monitor':
                if (kkk+1) == 1 or (kkk+1) % 10 == 0:
                    lr_scheduler.step(epoch + kkk / len(train_loader))
            optimizer.zero_grad()

            if len(data) > 3:
                videos, aug_masks, lambdas, list_id = data
                aug_masks = aug_masks.to(device)
                lambdas = lambdas.to(device)
            elif len(data) > 2:
                videos, orig_videos, list_id = data
                orig_videos = orig_videos.to(device)
                list_id = list_id.to(device)
                videos = videos.view((-1,config.data.num_segments,3)+videos.size()[-2:])
            else:
                videos, list_id = data
                videos = videos.view((-1,config.data.num_segments,3)+videos.size()[-2:])
            
            b,t,c,h,w = videos.size()
            
            text_id = numpy.random.randint(num_text_aug,size=len(list_id))
            texts = torch.stack([text_dict[j][i,:] for i,j in zip(list_id,text_id)])

            videos= videos.to(device).view(-1,c,h,w ) # omit the Image.fromarray if the images already in PIL format, change this line to images=list_image if using preprocess inside the dataset class
            texts = texts.to(device)


            ############## Calculate explainability loss between attn and mask ###########
            # run through the prompt model and get the new results from the prompts
            if not config.data.use_orig:
                loss_all, image_embedding, text_embedding = bounding_box_loss(criterion_list, b, t, c, h, w, list_id, aug_masks, lambdas, texts, promptCrit, fusion_model)
            else:
                video_tensor = videos.view(-1,c,h,w )
                image_embedding = model_image(video_tensor)
                image_embedding = image_embedding.view(b,t,-1)
                image_embedding = fusion_model(image_embedding)

                video_tensor = orig_videos.view(-1,c,h,w )
                image_emb_orig = model_image(video_tensor)
                image_emb_orig = image_emb_orig.view(b,t,-1)
                image_emb_orig = fusion_model(image_emb_orig)

                text_embedding = model_text(texts)

            if config.network.fix_text:
                text_embedding.detach_()

            logit_scale = perceptor.logit_scale.exp()
            logits_per_image, logits_per_text = create_logits(image_embedding,text_embedding,logit_scale)
            if config.data.use_orig:
                logits_per_image_orig, logits_per_text_orig = create_logits(image_emb_orig,text_embedding,logit_scale)
            
            if lambda_ce != 0:
                text_inputs = classes.to(device)
                text_features2 = perceptor.encode_text(text_inputs)
                logits_per_image2 = (100.0 * image_embedding @ text_features2.T)
                logits_per_image2_orig = (100.0 * image_emb_orig @ text_features2.T)
                similarity = calculate_similarity(logits_per_image2, b, num_text_aug)
                similarity_orig = calculate_similarity(logits_per_image2_orig, b, num_text_aug)
                list_id = list_id.to(device)
                ce_loss = cross_entropy(similarity+similarity_orig, list_id)

            # parameter = nn.Parameter()
            ground_truth = torch.tensor(gen_label(list_id),dtype=image_embedding.dtype,device=device)
            loss_imgs = loss_img(logits_per_image,ground_truth)
            loss_texts = loss_txt(logits_per_text,ground_truth)

            if config.data.use_orig:
                loss_imgs_orig = loss_img(logits_per_image_orig, ground_truth)
                loss_texts_orig = loss_txt(logits_per_text_orig,ground_truth)

                kl_loss = (loss_imgs + loss_texts)/2
                kl_loss_orig = ((loss_imgs_orig + loss_texts_orig)/2)
                kl_loss = config.data.lambda_cropped*kl_loss + config.data.lambda_orig*kl_loss_orig
            else:
                kl_loss = (loss_imgs + loss_texts)/2
            total_loss = kl_loss
            running_kl += kl_loss.item()
            if lambda_bb > 0:
                total_loss += lambda_bb*loss_all
                running_loss_all += loss_all.item()
            if lambda_ce > 0:
                total_loss += lambda_ce*ce_loss
                running_ce += ce_loss.item()
            running_total += total_loss.item()

            wandb.log({"train_total_loss": total_loss})
            wandb.log({"train_loss_imgs": loss_imgs})
            wandb.log({"train_loss_texts": loss_texts})
            wandb.log({"lr": optimizer.param_groups[0]['lr']})
            total_loss.backward()
            # print(f'Iter {kkk}: {total_loss.item()}')

            if device == "cpu":
                optimizer.step()
                # optimizer.step()
            else:
                convert_models_to_fp32(perceptor)
                optimizer.step()
                # optimizer.step()
                clip.model.convert_weights(perceptor)

            # scaler.update()

        if epoch % config.logging.eval_freq == 0:  # and epoch>0
            prec1 = validate(epoch,val_loader, classes, device, perceptor,fusion_model, config,num_text_aug)

        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        print(f'Testing: {prec1}/{best_prec1}, Avg Loss KL: {running_kl/len(train_loader)}, Avg Loss All: {running_loss_all/len(train_loader)}, Avg CE Loss: {running_ce/len(train_loader)}, Avg TOTAL: {running_total/len(train_loader)}')
        print('Saving:')
        filename = "{}/last_model.pt".format(working_dir)

        epoch_saving(epoch, perceptor, fusion_model, optimizer, filename)
        if is_best:
            best_saving(working_dir, epoch, perceptor, fusion_model, optimizer)
def main():
    global args, best_prec1
    global global_step
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-cfg', default='')
    parser.add_argument('--log_time', default='')
    args = parser.parse_args()
    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)
    working_dir = os.path.join('./exp', config['network']['type'], config['network']['arch'], config['data']['dataset'], args.log_time)
    
    seed = int(config['seed'])
    torch.manual_seed(seed) 
    torch.cuda.manual_seed(seed)  # For GPU operations
    torch.cuda.manual_seed_all(seed)  # If using multiple GPUs
    random.seed(seed)
    numpy.random.seed(seed) 

    wandb.init(project=config['network']['type'],name='{}_{}_{}_{}'.format(args.log_time,config['network']['type'], config['network']['arch'], config['data']['dataset']))
    print('-' * 80)
    print(' ' * 20, "working dir: {}".format(working_dir))
    print('-' * 80)

    print('-' * 80)
    print(' ' * 30, "Config")
    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(config)
    print('-' * 80)

    config = DotMap(config)

    Path(working_dir).mkdir(parents=True, exist_ok=True)
    shutil.copy(args.config, working_dir)
    shutil.copy('train.py', working_dir)

    device = "cuda" if torch.cuda.is_available() else "cpu" # If using GPU then use mixed precision training.

    perceptor, clip_state_dict = clip.load(config.network.arch,
                                           device=device,
                                           jit=False,
                                           tsm=config.network.tsm, 
                                           T=config.data.num_segments,
                                           dropout=config.network.drop_out, 
                                           emb_dropout=config.network.emb_dropout,
                                           pretrain=config.network.init, 
                                           joint = config.network.joint) #Must set jit=False for training  ViT-B/32
    transform_train = get_augmentation(True,config)
    transform_val = get_augmentation(False,config)

    if config.data.randaug.N > 0:
        transform_train = randAugment(transform_train, config)


    # print('train transforms: {}'.format(transform_train.transforms))
    # print('val transforms: {}'.format(transform_val.transforms))

    fusion_model = visual_prompt(config.network.sim_header,clip_state_dict,config.data.num_segments)
    model_text = TextCLIP(perceptor)
    model_image = ImageCLIP(perceptor)
    # model_stan = stanet_af(layers=[2, 2, 2, 2], in_channels=2, num_classes=embed_dim, k=2, features=16)
    model_text = torch.nn.DataParallel(model_text).cuda()
    model_image = torch.nn.DataParallel(model_image).cuda()
    fusion_model = torch.nn.DataParallel(fusion_model).cuda()
    wandb.watch(perceptor)
    wandb.watch(fusion_model)

    mask_transform = get_mask_augmentation(cut_size=224, 
                                        cutn=1, 
                                        cut_pow=1., 
                                        noise_fac = 0.1)
    if not config.data.use_orig:
        def collate_fn(batch):
            videos, masks, lambda_val, labels = zip(*batch)
            # Check the labels for bb
            videos, labels = torch.stack(videos), torch.tensor(labels)
            lambda_val = torch.tensor(lambda_val)
            masks = torch.stack(masks, dim=0)
            # videos = videos.view((-1,config.data.num_segments,3)+videos.size()[-2:])
            # masks = masks.view((-1,config.data.num_segments,3)+masks.size()[-2:])
            masks = masks.squeeze(dim=1)
            videos = videos.squeeze(dim=1)
            data = {'videos': videos, 'masks': masks}
            iii, aug_masks = mask_transform(data)
            # iii = iii.squeeze()
            return videos, aug_masks, lambda_val, labels

        train_data = Action_DATASETS(
                        config.data.train_list,
                        config.data.label_list,
                        num_segments=config.data.num_segments,
                        image_tmpl=config.data.image_tmpl,
                        random_shift=config.data.random_shift,
                        image_transform=transform_train)
        train_loader = DataLoader(
                        train_data,
                        batch_size=config.data.batch_size,
                        num_workers=config.data.workers,
                        shuffle=True,
                        pin_memory=False,
                        drop_last=True, 
                        collate_fn=collate_fn)
        val_data = Action_DATASETS(
                        config.data.val_list,
                        config.data.label_list, 
                        random_shift=False,
                        num_segments=config.data.num_segments,
                        image_tmpl=config.data.image_tmpl,
                        image_transform=transform_val)
        val_loader = DataLoader(
                        val_data,
                        batch_size=config.data.batch_size,
                        num_workers=config.data.workers,
                        shuffle=False,
                        pin_memory=False,
                        drop_last=True,
                        collate_fn=collate_fn)
    else:
        def collate_fn(batch):
            cropped_videos, images, bbs, labels = zip(*batch)
            # Check the labels for bb
            cropped_videos = torch.stack(cropped_videos) 
            images = torch.stack(images) 
            labels = torch.tensor(labels)
            return cropped_videos, images, labels

        train_data = Action_DATASETS_orig(
                        config.data.train_list,
                        config.data.label_list,
                        num_segments=config.data.num_segments,
                        image_tmpl=config.data.image_tmpl,
                        random_shift=config.data.random_shift,
                        transform=transform_train)
        train_loader = DataLoader(
                        train_data,
                        batch_size=config.data.batch_size,
                        num_workers=config.data.workers,
                        shuffle=True,
                        pin_memory=False,
                        drop_last=True, 
                        collate_fn=collate_fn)
        val_data = Action_DATASETS_orig(
                        config.data.val_list,
                        config.data.label_list, 
                        random_shift=False,
                        num_segments=config.data.num_segments,
                        image_tmpl=config.data.image_tmpl,
                        transform=transform_val)
        val_loader = DataLoader(
                        val_data,
                        batch_size=config.data.batch_size,
                        num_workers=config.data.workers,
                        shuffle=False,
                        pin_memory=False,
                        drop_last=True,
                        collate_fn=collate_fn)


    if device == "cpu":
        model_text.float()
        model_image.float()
    else :
        clip.model.convert_weights(model_text) # Actually this line is unnecessary since clip by default already on float16
        clip.model.convert_weights(model_image)

    loss_img = KLLoss()
    loss_txt = KLLoss()

    start_epoch = config.solver.start_epoch
    
    if config.pretrain:
        if os.path.isfile(config.pretrain):
            print(("=> loading checkpoint '{}'".format(config.pretrain)))
            checkpoint = torch.load(config.pretrain)
            perceptor.load_state_dict(checkpoint['model_state_dict'])
            fusion_model.load_state_dict(checkpoint['fusion_model_state_dict'])
            del checkpoint
        else:
            print(("=> no checkpoint found at '{}'".format(config.resume)))
    
    if config.resume:
        if os.path.isfile(config.resume):
            print(("=> loading checkpoint '{}'".format(config.resume)))
            checkpoint = torch.load(config.resume)
            perceptor.load_state_dict(checkpoint['model_state_dict'])
            fusion_model.load_state_dict(checkpoint['fusion_model_state_dict'])
            start_epoch = checkpoint['epoch']
            print(("=> loaded checkpoint '{}' (epoch {})"
                   .format(config.evaluate, start_epoch)))
            del checkpoint
        else:
            print(("=> no checkpoint found at '{}'".format(config.pretrain)))

    classes, num_text_aug, text_dict = text_prompt(train_data, file_name=config.prompt)

    replace_grad = ReplaceGrad.apply
    # 1. Compile a proper list of the classes with label numbers
    # 3. Create a replace_grad
    
    # 2. Find what the perceptor is for me
    # label_dict = {idx:class_name for idx, class_name in enumerate(train_data.classes)}
    if config.data.lambda_bb > 0:
        criterion_list = create_prompt_loss_dict(train_data.classes, perceptor, replace_grad, device)
    else:
        criterion_list = []

    best_prec1 = 0.0
    if config.solver.evaluate:
        prec1 = validate(start_epoch,val_loader, classes, device, perceptor,fusion_model, config,num_text_aug)
        return

    # for k,v in model.named_parameters():
    #     print('{}: {}'.format(k, v.requires_grad))

    optimizer = _optimizer(config, perceptor, fusion_model)
    lr_scheduler = _lr_scheduler(config, optimizer)

    promptCrit = pl2(perceptor, replace_grad, im_emb_type=config.data.im_emb_type).to(device)

    train_classifier(start_epoch = start_epoch, 
                     loss_img = loss_img,
                     loss_txt = loss_txt,
                     criterion_list = criterion_list,
                     promptCrit = promptCrit,
                     lr_scheduler = lr_scheduler,
                     config = config, 
                     text_dict = text_dict,
                     model_image = model_image, 
                     model_text = model_text, 
                     fusion_model = fusion_model, 
                     train_loader = train_loader, 
                     val_loader = val_loader, 
                     optimizer = optimizer, 
                     num_text_aug = num_text_aug,
                     classes = classes,
                     perceptor = perceptor,
                     working_dir = working_dir,
                     device = device,
                     lambda_bb=config.data.lambda_bb,
                     lambda_ce=config.data.lambda_ce)


if __name__ == '__main__':
    main()
