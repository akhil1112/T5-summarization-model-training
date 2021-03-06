# -*- coding: utf-8 -*-
"""transformers-summarization-t5.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1ZmNnH1oUrhgIl5GhSUWSdaWtrIfqEHQI

# Fine Tuning Transformer for Summary Generation
"""

!pip install transformers -q

# Code for TPU packages install
# !curl -q https://raw.githubusercontent.com/pytorch/xla/master/contrib/scripts/env-setup.py -o pytorch-xla-env-setup.py
# !python pytorch-xla-env-setup.py --apt-packages libomp5 libopenblas-dev

# Importing stock libraries
import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, RandomSampler, SequentialSampler

# Importing the T5 modules from huggingface/transformers
from transformers import T5Tokenizer, T5ForConditionalGeneration

# Checking out the GPU we have access to. This is output is from the google colab version. 
!nvidia-smi

# # Setting up the device for GPU usage
from torch import cuda
device = 'cuda' if cuda.is_available() else 'cpu'

# Preparing for TPU usage
# import torch_xla
# import torch_xla.core.xla_model as xm
# device = xm.xla_device()

"""
### Preparing the Dataset for data processing: Class

We will start with creation of Dataset class - This defines how the text is pre-processed before sending it to the neural network. This dataset will be used the the Dataloader method that will feed  the data in batches to the neural network for suitable training and processing. 
The Dataloader and Dataset will be used inside the `main()`.
Dataset and Dataloader are constructs of the PyTorch library for defining and controlling the data pre-processing and its passage to neural network. For further reading into Dataset and Dataloader read the [docs at PyTorch](https://pytorch.org/docs/stable/data.html)

#### *CustomDataset* Dataset Class
- This class is defined to accept the Dataframe as input and generate tokenized output that is used by the **T5** model for training. 
- We are using the **T5** tokenizer to tokenize the data in the `text` and `ctext` column of the dataframe. 
- The tokenizer uses the ` batch_encode_plus` method to perform tokenization and generate the necessary outputs, namely: `source_id`, `source_mask` from the actual text and `target_id` and `target_mask` from the summary text.
- To read further into the tokenizer, [refer to this document](https://huggingface.co/transformers/model_doc/t5.html#t5tokenizer)
- The *CustomDataset* class is used to create 2 datasets, for training and for validation.
- *Training Dataset* is used to fine tune the model: **80% of the original data**
- *Validation Dataset* is used to evaluate the performance of the model. The model has not seen this data during training. 

#### Dataloader: Called inside the `main()`
- Dataloader is used to for creating training and validation dataloader that load data to the neural network in a defined manner. This is needed because all the data from the dataset cannot be loaded to the memory at once, hence the amount of data loaded to the memory and then passed to the neural network needs to be controlled.
- This control is achieved using the parameters such as `batch_size` and `max_len`.
- Training and Validation dataloaders are used in the training and validation part of the flow respectively
"""

# Creating a custom dataset for reading the dataframe and loading it into the dataloader to pass it to the neural network at a later stage for finetuning the model and to prepare it for predictions

class CustomDataset(Dataset):

    def __init__(self, dataframe, tokenizer, source_len, summ_len):
        self.tokenizer = tokenizer
        self.data = dataframe
        self.source_len = source_len
        self.summ_len = summ_len
        self.text = self.data.text
        self.ctext = self.data.ctext

    def __len__(self):
        return len(self.text)

    def __getitem__(self, index):
        ctext = str(self.ctext[index])
        ctext = ' '.join(ctext.split())

        text = str(self.text[index])
        text = ' '.join(text.split())

        source = self.tokenizer.batch_encode_plus([ctext], max_length= self.source_len, pad_to_max_length=True,return_tensors='pt')
        target = self.tokenizer.batch_encode_plus([text], max_length= self.summ_len, pad_to_max_length=True,return_tensors='pt')

        source_ids = source['input_ids'].squeeze()
        source_mask = source['attention_mask'].squeeze()
        target_ids = target['input_ids'].squeeze()
        target_mask = target['attention_mask'].squeeze()

        return {
            'source_ids': source_ids.to(dtype=torch.long), 
            'source_mask': source_mask.to(dtype=torch.long), 
            'target_ids': target_ids.to(dtype=torch.long),
            'target_ids_y': target_ids.to(dtype=torch.long)
        }

"""
### Fine Tuning the Model: Function

Here we define a training function that trains the model on the training dataset created above, specified number of times (EPOCH), An epoch defines how many times the complete data will be passed through the network. 

This function is called in the `main()`

Following events happen in this function to fine tune the neural network:
- The epoch, tokenizer, model, device details, testing_ dataloader and optimizer are passed to the `train ()` when its called from the `main()`
- The dataloader passes data to the model based on the batch size.
- `language_model_labels` are calculated from the `target_ids` also, `source_id` and `attention_mask` are extracted.
- The model outputs first element gives the loss for the forward pass. 
- Loss value is used to optimize the weights of the neurons in the network.
- After every 10 steps the loss value is logged in the wandb service.
- After every 500 steps the loss value is printed in the console.
"""

# Creating the training function. This will be called in the main function. It is run depending on the epoch value.
# The model is put into train mode and then we wnumerate over the training loader and passed to the defined network 

def train(epoch, tokenizer, model, device, loader, optimizer):
    model.train()
    for _,data in enumerate(loader, 0):
        y = data['target_ids'].to(device, dtype = torch.long)
        y_ids = y[:, :-1].contiguous()
        lm_labels = y[:, 1:].clone().detach()
        lm_labels[y[:, 1:] == tokenizer.pad_token_id] = -100
        ids = data['source_ids'].to(device, dtype = torch.long)
        mask = data['source_mask'].to(device, dtype = torch.long)

        outputs = model(input_ids = ids, attention_mask = mask, decoder_input_ids=y_ids, lm_labels=lm_labels)
        loss = outputs[0]
        
        if _%500==0:
            print(f'Epoch: {epoch}, Loss:  {loss.item()}')
        
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        # xm.optimizer_step(optimizer)
        # xm.mark_step()

"""
### Validating the Model Performance: Function

During the validation stage we pass the unseen data(Testing Dataset), trained model, tokenizer and device details to the function to perform the validation run. This step generates new summary for dataset that it has not seen during the training session. 

This function is called in the `main()`

This unseen data is the 20% of `news_summary.csv` which was seperated during the Dataset creation stage. 
During the validation stage the weights of the model are not updated. We use the generate method for generating new text for the summary. 

It depends on the `Beam-Search coding` method developed for sequence generation for models with LM head. 

The generated text and originally summary are decoded from tokens to text and returned to the `main()`
"""

def validate(epoch, tokenizer, model, device, loader):
    model.eval()
    predictions = []
    actuals = []
    with torch.no_grad():
        for _, data in enumerate(loader, 0):
            y = data['target_ids'].to(device, dtype = torch.long)
            ids = data['source_ids'].to(device, dtype = torch.long)
            mask = data['source_mask'].to(device, dtype = torch.long)

            generated_ids = model.generate(
                input_ids = ids,
                attention_mask = mask, 
                max_length=150, 
                num_beams=2,
                repetition_penalty=2.5, 
                length_penalty=1.0, 
                early_stopping=True
                )
            preds = [tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=True) for g in generated_ids]
            target = [tokenizer.decode(t, skip_special_tokens=True, clean_up_tokenization_spaces=True)for t in y]
            if _%100==0:
                print(f'Completed {_}')

            predictions.extend(preds)
            actuals.extend(target)
    return predictions, actuals

### Main Function

#The `main()` as the name suggests is the central location to execute all the functions/flows created above in the notebook. The following steps are executed in the `main()`:

#### Importing and Pre-Processing the domain data

#We will be working with the data and preparing it for fine tuning purposes. 
#Assuming that the `news_summary.csv` is already downloaded in your `data` folder
# Cleaning the file to remove the unwanted columns.
# A new string is added to the main article column `summarize: ` prior to the actual article. This is done because **T5** had similar formatting for the summarization dataset. 

#### Creation of Dataset and Dataloader

# The updated dataframe is divided into 80-20 ratio for test and validation. 
# Both the data-frames are passed to the `CustomerDataset` class for tokenization of the new articles and their summaries.
# The tokenization is done using the length parameters passed to the class.
# Train and Validation parameters are defined and passed to the `pytorch Dataloader contstruct` to create `train` and `validation` data loaders.
# These dataloaders will be passed to `train()` and `validate()` respectively for training and validation action.

#### Neural Network and Optimizer

# In this stage we define the model and optimizer that will be used for training and to update the weights of the network. 
# We are using the `t5-base` transformer model for our project. You can read about the `T5 model` and its features above. 
# We use the `T5ForConditionalGeneration.from_pretrained("t5-base")` commad to define our model. The `T5ForConditionalGeneration` adds a Language Model head to our `T5 model`. The Language Model head allows us to generate text based on the training of `T5 model`.
# We are using the `Adam` optimizer for our project. This has been a standard for all our tutorials and is something that can be changed updated to see how different optimizer perform with different learning rates. 
# There is also a scope for doing more with Optimizer such a decay, momentum to dynamically update the Learning rate and other parameters. All those concepts have been kept out of scope for these tutorials. 


#### Training Model

# We call the `train()` with all the necessary parameters.
# Loss at every 500th step is printed on the console.

#### Validation and generation of Summary

# After the training is completed, the validation step is initiated.
# As defined in the validation function, the model weights are not updated. We use the fine tuned model to generate new summaries based on the article text.
# An output is printed on the console giving a count of how many steps are complete after every 100th step. 
# The original summary and generated summary are converted into a list and returned to the main function. 
# Both the lists are used to create the final dataframe with 2 columns **Generated Summary** and **Actual Summary**
# The dataframe is saved as a csv file in the local drive.
# A qualitative analysis can be done with the Dataframe. 


def main():

    TRAIN_BATCH_SIZE = 2    # input batch size for training (default: 64)
    VALID_BATCH_SIZE = 2    # input batch size for testing (default: 1000)
    TRAIN_EPOCHS = 2        # number of epochs to train (default: 10)
    VAL_EPOCHS = 1 
    LEARNING_RATE = 1e-4    # learning rate (default: 0.01)
    SEED = 42               # random seed (default: 42)
    MAX_LEN = 512
    SUMMARY_LEN = 150 

    # Set random seeds and deterministic pytorch for reproducibility
    torch.manual_seed(SEED) # pytorch random seed
    np.random.seed(SEED) # numpy random seed
    torch.backends.cudnn.deterministic = True

    # tokenzier for encoding the text
    tokenizer = T5Tokenizer.from_pretrained("t5-base")
    

    # Importing and Pre-Processing the domain data
    # Selecting the needed columns only. 
    # Adding the summarzie text in front of the text. This is to format the dataset similar to how T5 model was trained for summarization task. 
    df = pd.read_csv('../input/news-summary/news_summary.csv',encoding='latin-1')
    df = df[['text','ctext']]
    df.ctext = 'summarize: ' + df.ctext
    print(df.head())

    
    # Creation of Dataset and Dataloader
    # Defining the train size. So 80% of the data will be used for training and the rest will be used for validation. 
    train_size = 0.8
    train_dataset=df.sample(frac=train_size, random_state = SEED).reset_index(drop=True)
    val_dataset=df.drop(train_dataset.index).reset_index(drop=True)

    print("FULL Dataset: {}".format(df.shape))
    print("TRAIN Dataset: {}".format(train_dataset.shape))
    print("TEST Dataset: {}".format(val_dataset.shape))


    # Creating the Training and Validation dataset for further creation of Dataloader
    training_set = CustomDataset(train_dataset, tokenizer, MAX_LEN, SUMMARY_LEN)
    val_set = CustomDataset(val_dataset, tokenizer, MAX_LEN, SUMMARY_LEN)

    # Defining the parameters for creation of dataloaders
    train_params = {
        'batch_size': TRAIN_BATCH_SIZE,
        'shuffle': True,
        'num_workers': 0
        }

    val_params = {
        'batch_size': VALID_BATCH_SIZE,
        'shuffle': False,
        'num_workers': 0
        }

    # Creation of Dataloaders for testing and validation. This will be used down for training and validation stage for the model.
    training_loader = DataLoader(training_set, **train_params)
    val_loader = DataLoader(val_set, **val_params)


    
    # Defining the model. We are using t5-base model and added a Language model layer on top for generation of Summary. 
    # Further this model is sent to device (GPU/TPU) for using the hardware.
    model = T5ForConditionalGeneration.from_pretrained("t5-base")
    model = model.to(device)

    # Defining the optimizer that will be used to tune the weights of the network in the training session. 
    optimizer = torch.optim.Adam(params =  model.parameters(), lr=LEARNING_RATE)

    # Training loop
    print('Initiating Fine-Tuning for the model on our dataset')

    for epoch in range(TRAIN_EPOCHS):
        train(epoch, tokenizer, model, device, training_loader, optimizer)


    # Validation loop and saving the resulting file with predictions and acutals in a dataframe.
    # Saving the dataframe as predictions.csv
    print('Now generating summaries on our fine tuned model for the validation dataset and saving it in a dataframe')
    for epoch in range(VAL_EPOCHS):
        predictions, actuals = validate(epoch, tokenizer, model, device, val_loader)
        final_df = pd.DataFrame({'Generated Text':predictions,'Actual Text':actuals})
        final_df.to_csv('predictions.csv')
        print('Output Files generated for review')

if __name__ == '__main__':
    main()


