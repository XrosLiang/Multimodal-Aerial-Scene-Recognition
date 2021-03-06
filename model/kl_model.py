import sys
sys.path.append("../resnet-audio/")
sys.path.append("../resnet-image/")
sys.path.append("../data/")
import torch
from torch.utils.data import DataLoader,TensorDataset
import argparse
from CVS_dataset import CVSDataset
from resnet_image import resnet101 as IMG_NET
from resnet_audio import resnet50  as AUD_NET
from fusion_net import FusionNet as FUS_NET
from fusion_net import FusionNet_KD
import pickle
import torch.optim as optim
import datetime
import os
import sklearn
from data_partition import data_construction
import numpy as np

def net_test(net,data_loader,cal_map=False):
    num = len(data_loader.dataset)
    correct = 0
    net.eval()
    predict_labels = np.array([])
    ground_labels  = np.array([])
    with torch.no_grad():
        for i, data in enumerate(data_loader, 0):
            img, aud, label, _e, _r = data
            img, aud, label = img.type(torch.FloatTensor).cuda(), aud.type(torch.FloatTensor).cuda(), label.type(torch.LongTensor).cuda() # gpu
            output, _ = net(img, aud)
            predict_label = torch.argmax(output, dim=1)
            #if predict_labels == None:
            #    predict_labels = predict_label.cpu().numpy()
            #else:
            predict_labels = np.concatenate((predict_labels, predict_label.cpu().numpy()))
            #if ground_labels == None:
            #    ground_labels = label.cpu().numpy()
            #else:
            ground_labels = np.concatenate((ground_labels, label.cpu().numpy()))
            correct += ((predict_label == label).sum().cpu().numpy())
            # map

    
    (precision, recall, fscore, sup) = sklearn.metrics.precision_recall_fscore_support(ground_labels, predict_labels, average='weighted')
    acc = correct/num

    return (acc, precision, recall, fscore)


def decrease_learning_rate(optimizer, decay_factor=0.1):
    for param_group in optimizer.param_groups:
        param_group['lr'] *= decay_factor


def main():
    parser = argparse.ArgumentParser(description='AID_PRETRAIN')
    parser.add_argument('--dataset_dir', type=str, default='F:\\download\\CVS_Dataset_New\\', help='the path of the dataset')
    parser.add_argument('--batch_size', type=int, default=64,help='training batch size')
    parser.add_argument('--learning_rate', type=float, default=1e-4, help='training batch size')
    parser.add_argument('--epoch',type=int,default=2000,help='training epoch')
    parser.add_argument('--gpu_ids', type=str, default='[0,1,2,3]', help='USING GPU IDS e.g.\'[0,4]\'')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M', help = 'SGD momentum (default: 0.9)')
    parser.add_argument('--image_net_weights', type=str, default='visual_model_pretrain.pt', help='image net weights')
    parser.add_argument('--audio_net_weights', type=str, default='checkpoint59.pt',
                        help='image net weights')

    parser.add_argument('--data_dir', type=str, default='/mnt/scratch/hudi/soundscape/data/',
                        help='image net weights')
    parser.add_argument('--num_threads', type=int, default=8, help='number of threads')
    parser.add_argument('--data_name', type=str, default='CVS_data_ind.pkl')
    parser.add_argument('--seed', type=int, default=10)
    parser.add_argument('--audionet_pretrain', type=int, default=0)
    parser.add_argument('--videonet_pretrain', type=int, default=0) 
    parser.add_argument('--kd_weight', type=float, default=0.1)

    args = parser.parse_args()

    print('kl_model...')
    print('baseline...')
    print('kd_weight ' + str(args.kd_weight))
    print('audionet_pretrain ' + str(args.audionet_pretrain))
    print('videonet_pretrain ' + str(args.videonet_pretrain))

    (train_sample, train_label, val_sample, val_label, test_sample, test_label) = data_construction(args.data_dir)

    #f = open(args.data_name, 'wb')
    #data = {'train_sample':train_sample, 'train_label':train_label, 'test_sample':test_sample, 'test_label':test_label}
    #pickle.dump(data, f)
    #f.close()

    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)


    train_dataset = CVSDataset(args.data_dir, train_sample, train_label, seed=args.seed, event_label_name='event_label_bayes_59')
    val_dataset  = CVSDataset(args.data_dir, val_sample, val_label, seed=args.seed, event_label_name='event_label_bayes_59')
    test_dataset = CVSDataset(args.data_dir, test_sample, test_label, seed=args.seed, event_label_name='event_label_bayes_59')

    train_dataloader = DataLoader(dataset=train_dataset, batch_size=args.batch_size,shuffle=False, num_workers=args.num_threads)
    val_dataloader   = DataLoader(dataset=val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.num_threads)
    test_dataloader  = DataLoader(dataset=test_dataset, batch_size=args.batch_size,shuffle=False, num_workers=args.num_threads)

    image_net = IMG_NET(num_classes=30)
    if args.videonet_pretrain:
        state = torch.load(args.image_net_weights)
        image_net.load_state_dict(state)

    audio_net = AUD_NET()
    if args.audionet_pretrain:
        state = torch.load(args.audio_net_weights)['model']
        audio_net.load_state_dict(state)

    # all stand up
    fusion_net = FusionNet_KD(image_net, audio_net, num_classes=13)
    

    gpu_ids = [i for i in range(4)]
    fusion_net_cuda = torch.nn.DataParallel(fusion_net, device_ids=gpu_ids).cuda()

    loss_func_CE  = torch.nn.CrossEntropyLoss()
    loss_func_BCE = torch.nn.BCELoss(reduce=True)
    loss_func_MSE = torch.nn.MSELoss()

    optimizer = optim.Adam(params=fusion_net_cuda.parameters(), lr=args.learning_rate, betas=(0.9, 0.999), weight_decay=0.0001)

    max_fscore = 0.
    count = 0
    for e in range(args.epoch):

        fusion_net_cuda.train()
        begin_time = datetime.datetime.now()

        scene_loss = 0.0
        event_loss = 0.0
        batch_num = int(len(train_dataloader.dataset) / args.batch_size)

        for i, data in enumerate(train_dataloader, 0):
            # print('batch:%d/%d' % (i,batch_num))
            img, aud, scene_label, event_label, _ = data
            img, aud, scene_label, event_label = img.type(torch.FloatTensor).cuda(), aud.type(torch.FloatTensor).cuda(), scene_label.type(
                torch.LongTensor).cuda(), event_label.type(torch.FloatTensor).cuda()

            optimizer.zero_grad()

            scene_output, KD_output = fusion_net_cuda(img, aud)
            CE_loss  = loss_func_CE(scene_output, scene_label)
            BCE_loss = loss_func_BCE(KD_output, event_label) * args.kd_weight

            #CE_loss.backward(retain_graph=True)
            #MSE_loss.backward()
            losses = CE_loss + BCE_loss
            losses.backward()
            optimizer.step()

            scene_loss  += CE_loss.cpu()
            event_loss  += BCE_loss.cpu()

        end_time = datetime.datetime.now()
        delta_time = (end_time - begin_time)
        delta_seconds = (delta_time.seconds * 1000 + delta_time.microseconds) / 1000


        (val_acc, val_precision, val_recall, val_fscore) = net_test(fusion_net_cuda, val_dataloader)
        print('epoch:%d scene loss:%.4f event loss:%.4f val acc:%.4f val_precision:%.4f val_recall:%.4f val_fscore:%.4f ' % (e, scene_loss.cpu(), event_loss.cpu(), val_acc, val_precision, val_recall, val_fscore))
        if val_fscore > max_fscore:
            count = 0
            max_fscore = val_fscore
            (test_acc, test_precision, test_recall, test_fscore) = net_test(fusion_net_cuda, test_dataloader)
            test_acc_list = [test_acc]
            test_precision_list = [test_precision]
            test_recall_list = [test_recall]
            test_fscore_list = [test_fscore]
            print('mark...') 
            #print('test acc:%.4f precision:%.4f recall:%.4f fscore:%.4f' % (test_acc, test_precision, test_recall, test_fscore))
        else:
            count = count + 1
            (test_acc, test_precision, test_recall, test_fscore) = net_test(fusion_net_cuda, test_dataloader)
           
            test_acc_list.append(test_acc)
            test_precision_list.append(test_precision)
            test_recall_list.append(test_recall)
            test_fscore_list.append(test_fscore)
        
        if count == 5:
            test_acc_mean = np.mean(test_acc_list)
            test_acc_std  = np.std(test_acc_list)

            test_precision_mean = np.mean(test_precision_list)
            test_precision_std  = np.std(test_precision_list)

            test_recall_mean = np.mean(test_recall_list)
            test_recall_std = np.std(test_recall_list)

            test_fscore_mean = np.mean(test_fscore_list)
            test_fscore_std = np.std(test_fscore_list)
 
            print('test acc:%.4f (%.4f) precision:%.4f (%.4f) recall:%.4f (%.4f) fscore:%.4f(%.4f)' % (test_acc_mean, test_acc_std, test_precision_mean, test_precision_std, test_recall_mean, test_recall_std, test_fscore_mean, test_fscore_std))
            count = 0


        if e in [30, 60, 90]:
            decrease_learning_rate(optimizer, 0.1)
            print('decreased learning rate by 0.1')


if __name__ == '__main__':
    main()






