import torch
import torch.nn as nn
import torch.optim as optim
import torchvision.models as models
import torchvision.transforms as transforms
from torchvision.datasets import ImageFolder
from torch.utils.data import DataLoader, random_split

import argparse
import logging
import os
import sys
from tqdm import tqdm
from PIL import ImageFile

from smdebug import modes
import smdebug.pytorch as smd

ImageFile.LOAD_TRUNCATED_IMAGES = True
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler(sys.stdout))



def test(model, test_loader, criterion, device, hook):
    
    logger.info("start testing....")
    
    if hook is not None:
        hook.set_mode(modes.EVAL)
        
    model.eval()
    test_loss = 0
    correct = 0
    l = len(test_loader.dataset)
    with torch.no_grad():
        for data, target in test_loader:
            data = data.to(device)
            target = target.to(device)
            output = model(data)
            loss = criterion(output, target)
            test_loss += loss.item()* data.size(0)
            pred = output.max(1, keepdim = True)[1]
            correct += pred.eq(target.view_as(pred)).sum().item()
        test_loss /= l
        logger.info(
            "Test set: Accuracy: {:.2f} ({}/{}), Average loss: {:.4f} \n".format( 100.0 * correct / l, correct, l, test_loss)
        )


def train(model, train_loader, valid_loader, criterion, optimizer, epochs, device, hook, tol):
    logger.info("Training started.")
    l = len(train_loader.dataset)
    m = len(valid_loader.dataset)
    
    best_score=0
    best_model=None
    
    count=0
    
    for i in tqdm(range(epochs), desc="Training"):

        train_losses = 0
        correct_train = 0
        if hook is not None:
            hook.set_mode(modes.TRAIN)
            
        model.train()

        for data, target in train_loader:
            data = data.to(device)
            target = target.to(device)

            optimizer.zero_grad()

            outputs = model(data)

            train_loss = criterion(outputs, target)

            train_loss.backward()
            optimizer.step()

            train_losses += train_loss.item()* data.size(0)
            pred = outputs.max(1, keepdim = True)[1]
            correct_train += pred.eq(target.view_as(pred)).sum().item()

        train_losses /= l
        logger.info("Epoch {}: Train set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
            i+1, train_losses, correct_train, l, 100.0 * correct_train / l))

        val_losses = 0
        correct_val = 0
        
        if hook is not None:
            hook.set_mode(modes.EVAL)
            
        model.eval()

        with torch.no_grad():
            for data, target in valid_loader:
                data = data.to(device)
                target = target.to(device)

                outputs = model(data)

                val_loss = criterion(outputs, target)
                val_losses += val_loss.item()* data.size(0)
                pred = outputs.max(1, keepdim = True)[1]
                correct_val += pred.eq(target.view_as(pred)).sum().item()

        val_losses /= m
        score = 100.0 * correct_val / m
        logger.info("Epoch {}: Val set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
            i+1, val_losses, correct_val, m, score))
        
        if best_score<score:
            best_score=score
            best_model=model.state_dict()
            count=0
        else:
            count+=1
            if count==tol:
                logger.info("Early stopping.")
                model.load_state_dict(best_model)
                break

    logger.info("Training completed.")
    model.load_state_dict(best_model)
    
def net(model_name, num_classes, layers):
    
    logger.info("Model creation for fine-tuning started.")
    
    model = eval("models."+model_name)(pretrained=True, progress=True)
    
    for param in model.parameters():
        param.requires_grad = False

    num_features = model.fc.in_features
    
    full = [num_features,]+layers+[num_classes,]
    
    seq = list()
    
    for i in range(len(full)-2):
        seq.append(nn.Linear(full[i], full[i+1]))
        seq.append(nn.ReLU())
    
    seq.append(nn.Linear(full[-2], full[-1]))
    
    model.fc = nn.Sequential(*seq)

    logger.info("Model creation completed.")

    return model

def create_data_loaders(data_path, batch_size):

    train_transform = transforms.Compose([
        transforms.RandomResizedCrop((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    ])

    test_transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    ])

    data = ImageFolder(
        root=data_path,
        transform=train_transform
    )

    
    train_data, valid_data, test_data = random_split(data, [0.8,0.1,0.1])
    
    train_loader = DataLoader(
        train_data,
        batch_size=batch_size,
        shuffle=True,
    )
    
    valid_loader = DataLoader(
        valid_data,
        batch_size=batch_size,
        shuffle=False,
    )
    
    test_loader = DataLoader(
        test_data,
        batch_size=batch_size,
        shuffle=False,
    )

    return train_loader, valid_loader, test_loader

def main(args):
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    model = net(args.model_name, args.num_classes, args.layers)
    model = nn.DataParallel(model).to(device)
    
    loss_criterion = nn.CrossEntropyLoss()
    
    if args.do_hook:
        logger.info("Hook creation.")
        hook = smd.Hook.create_from_json_file()
        hook.register_module(model)
        hook.register_loss(loss_criterion)
    else:
        hook = None
    
    optimizer = optim.Adam(model.parameters(), lr=args.lr) 
    
    train_loader, valid_loader, test_loader = create_data_loaders(args.data_path, args.batch_size)
    
    train(model, train_loader, valid_loader, loss_criterion, optimizer, args.epochs, device, hook, args.tol)
     
    test(model, test_loader, loss_criterion, device, hook)
    
    logger.info("Saving Model")
    
    torch.save(model.state_dict(), os.path.join(args.model_dir, "model.pth"))

if __name__=='__main__':
    
    parser=argparse.ArgumentParser()
    
    parser.add_argument(
        "--model-name",
        type=str,
        default="resnet50",
        help="input torchvision model name (default: resnet50)",
    )
    
    parser.add_argument(
        '--num-layers',
        default=0,
        type=int,
        help='input layers sizes (default: 0)'
    )
    
    parser.add_argument(
        '--num-neurons',
        default=0,
        type=int,
        help='input layers sizes (default: 0)'
    )
    
    parser.add_argument(
        "--num-classes",
        type=int,
        default=5,
        help="input number of classes (default: 5)",
    )
    
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="input batch size for training (default: 64)",
    )

    parser.add_argument(
        "--epochs",
        type = int ,
        default = 100, 
        help = "number of epochs to train (default : 100)"
    )
    
    parser.add_argument(
        "--tol",
        type = int ,
        default = 10, 
        help = "number of epochs to tolerate before stop trainig when the score do not increase (default : 10)"
    )
    
    parser.add_argument(
        "--lr",
        type = float ,
        default = 0.001, 
        help = "learning rate (default : 0.001)"
    )
    
    
    parser.add_argument(
        "--do-hook",
        type = bool ,
        default = False,
        help = "performe debugging and profiling (default : False)"
    )
    
    parser.add_argument('--data_path', type=str, default=os.environ['SM_CHANNEL_TRAIN'])
    parser.add_argument('--model_dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--output_dir', type=str, default=os.environ['SM_OUTPUT_DATA_DIR'])
    
    args=parser.parse_args()
    
    args.layers= 2**args.num_layers*[2**args.num_neurons,]
    
    main(args)
