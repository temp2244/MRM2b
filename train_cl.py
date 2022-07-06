import torch
import torch.nn as nn
import os
import time
import numpy as np
import pandas as pd
from torch.utils.data import DataLoader
import json
from sklearn import preprocessing
from models_origin.OS_CNN import OS_CNN
from models_origin.FCN import FCN
from models_origin.ResNet import ResNet
from models_origin.RNN_FCN import MLSTM_FCN
from models_origin.InceptionTime import InceptionTime

from utils import train_test_supervised_batch, \
    readuea, train_supervised_batch_with_cl, CenterLoss
from config import multiflist, set_parser
from dataset import TSDataset

from OS_CNN_Structure_build import generate_layer_parameter_list

if __name__ == '__main__':
    train_config = set_parser()
    fid = train_config.start_dataset_id
    fname = multiflist[fid]
    out_dir = train_config.out_dir
    result_df_name = "{}_{}_{}_{}.csv".format(train_config.model_name.lower(), fid, fname, train_config.repeat)
    basename = "{}_{}_{}_{}".format(train_config.model_name.lower(), fid, fname, train_config.repeat)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    if not os.path.exists(os.path.join(out_dir, 'checkpoints')):
        os.makedirs(os.path.join(out_dir, 'checkpoints'))
    if not os.path.exists(os.path.join(out_dir, 'config')):
        os.makedirs(os.path.join(out_dir, 'config'))
    if not os.path.exists(os.path.join(out_dir, 'csv')):
        os.makedirs(os.path.join(out_dir, 'csv'))

    data_dir = train_config.data_dir
    device = torch.device('cuda:3')

    print("start process the data set {}:{}".format(fid, fname))
    print('results will write in the path: {}'.format(out_dir))
    # process the data
    x_train, y_train = readuea(data_dir, fname, 'TRAIN')
    x_test, y_test = readuea(data_dir, fname, 'TEST')
    x_train_num, x_test_num = x_train.shape[0], x_test.shape[0]
    print('train shape is: {}, test shape is: {}'.format(x_train.shape, x_test.shape))

    le = preprocessing.LabelEncoder().fit(y_train)
    y_train, y_test = le.transform(y_train), le.transform(y_test)
    nb_classes = len(np.unique(y_test))
    print('the number of classes is: {}'.format(nb_classes))

    batch_size = train_config.batch_size
    if len(x_train.shape) == 2 and x_train.shape[1] > 2048:
        batch_size = 32
    if len(x_train.shape) == 3 and x_train.shape[2] > 2048:
        batch_size = 32

    train_dataset = TSDataset(data=x_train, labels=y_train, is_train=True)
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
        pin_memory=True
    )

    test_dataset = TSDataset(data=x_test, labels=y_test, is_train=False)
    test_dataloader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
        pin_memory=True
    )

    # config for os_cnn
    Max_kernel_size = 89
    start_kernel_size = 1
    paramenter_number_of_layer_list = [8 * 128 * x_train.shape[1], 5 * 128 * 256 + 2 * 256 * 128]
    if train_config.model_name.lower() == 'fcn':
        model = FCN(int(x_train.shape[1]), nb_classes).to(device)
    elif train_config.model_name.lower() == 'mlstm_fcn':
        model = MLSTM_FCN(int(x_train.shape[1]), nb_classes, shuffle=False).to(device)
    elif train_config.model_name.lower() == 'resnet':
        model = ResNet(int(x_train.shape[1]), nb_classes).to(device)
    elif train_config.model_name.lower() == 'inceptiontime':
        model = InceptionTime(int(x_train.shape[1]), nb_classes).to(device)
    else:
        receptive_field_shape = min(int(x_train.shape[-1] / 4), Max_kernel_size)
        layer_parameter_list = generate_layer_parameter_list(start_kernel_size,
                                                             receptive_field_shape,
                                                             paramenter_number_of_layer_list,
                                                             in_channel=int(x_train.shape[1])
                                                             )
        model = OS_CNN(layer_parameter_list, nb_classes, False).to(device)
    criterion_cl = CenterLoss(num_classes=nb_classes, feat_dim=model.fc.in_features,device= device)
    criterion_mean = nn.CrossEntropyLoss(reduction='mean')

    params = list(model.parameters()) + list(criterion_cl.parameters())
    optimizer = torch.optim.Adam(params, lr=train_config.lr, weight_decay=train_config.weight_decay)

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode='min',
        factor=0.5,
        patience=50,
        verbose=True,
        min_lr=0.00005,
    )
    best_loss = 1e9
    final_res = [0, 0]
    for epoch in range(train_config.train_from_scratch_max_epoch):
        start = time.time()
        train_res_dict = train_test_supervised_batch(
            model,
            criterion_mean,
            train_dataloader,
            device,
            optimizer=optimizer,
            is_train=True,
            comparison_key='origin_out',
        )
        test_res_dict = train_test_supervised_batch(
            model,
            criterion_mean,
            test_dataloader,
            device,
            optimizer=optimizer,
            is_train=False,
            comparison_key='origin_out',
        )
        print('epoch [%4d/ %4d], test loss: %.6f, test acc %.6f, train loss: %.6f, train acc: %.6f, time: %f s\n' %
              (epoch + 1, train_config.train_from_scratch_max_epoch, test_res_dict['loss'], test_res_dict['acc'],
               train_res_dict['loss'], train_res_dict['acc'], time.time() - start))
        scheduler.step(train_res_dict['loss'])
        if best_loss > train_res_dict['loss']:
            best_loss = train_res_dict['loss']
            final_res = [test_res_dict['loss'], test_res_dict['acc']]
            torch.save(model.state_dict(), os.path.join(out_dir, 'checkpoints', "{}_best_model.pth".format(basename)))

    print('+++ In the first %4d epochs, the best training loss: %.6f, the test loss: %.6f, the test accuracy: %.6f \n' % (
        train_config.train_from_scratch_max_epoch, best_loss, final_res[0], final_res[1]))

    max_epoch = train_config.max_epoch
    print('the max epoch is: {}\n'.format(max_epoch))
    model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=train_config.lr_restart, weight_decay=train_config.weight_decay)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode='min',
        factor=0.5,
        patience=50,
        verbose=True,
        min_lr=0.00005,
    )

    best_loss = 1e9
    final_res = [0, 0]
    for epoch in range(train_config.train_from_scratch_max_epoch, max_epoch):
        start = time.time()
        train_res_dict = train_supervised_batch_with_cl(
            model,
            criterion_mean,
            criterion_cl,
            train_dataloader,
            device,
            train_config,
            optimizer=optimizer,
        )
        test_res_dict = train_test_supervised_batch(
            model,
            criterion_mean,
            test_dataloader,
            device,
            optimizer=optimizer,
            is_train=False,
            comparison_key='origin_out',
        )
        print('epoch [%4d/ %4d], test loss: %.6f, test acc %.6f, train sm loss: %.6f, train cl loss: %.6f, train acc: '
              '%.6f, time: %f s' %
            (epoch + 1, train_config.train_from_scratch_max_epoch, test_res_dict['loss'], test_res_dict['acc'],
             train_res_dict['origin_running_loss'], train_res_dict['cl_running_loss'], train_res_dict['acc'],
             time.time() - start))
        scheduler.step(train_res_dict['origin_running_loss'])
        if epoch > train_config.train_from_scratch_max_epoch + (
                train_config.max_epoch - train_config.train_from_scratch_max_epoch) * 0.5 and best_loss > \
                train_res_dict['origin_running_loss']:
            best_loss = train_res_dict['origin_running_loss']
            final_res = [test_res_dict['loss'], test_res_dict['acc']]
            torch.save(model.state_dict(), os.path.join(out_dir, 'checkpoints', "{}_best_cl_model.pth".format(basename)))

    print('W/ Center loss, The best training loss: %.6f, the test loss: %.6f, the test accuracy: %.6f' % (
        best_loss, final_res[0], final_res[1]))
    result_df = pd.DataFrame({
        "model": [train_config.model_name.lower()],
        'train_loss': [float("%.5f" % (best_loss))],
        'test_loss': [float("%.5f" % (final_res[0]))],
        'test_acc': [float("%.5f" % (final_res[1]))],
    })
    result_df.to_csv(os.path.join(out_dir, 'csv', result_df_name))
    json.dump(train_config.to_dict(),
              open(os.path.join(out_dir, 'config', '{}_train_config.json'.format(basename)), 'w'), indent=4)
