import numpy as np
from sklearn.metrics import accuracy_score,f1_score
from tqdm import trange
import torch
from torch.utils.data import TensorDataset, DataLoader
from transformers import BertModel, BertTokenizer, AdamW, BertPreTrainedModel, BertModel, get_linear_schedule_with_warmup

def log(message,file='keous_log.txt'):
    with open(file,'a+') as f:
        f.write(message)
    print(message)

device = torch.device('cuda')

class MyBert(BertPreTrainedModel):
    def __init__(self,config,num_classes=None,dropout_prob=None):
        super().__init__(config)
        self.bert = BertModel(config)
        if num_classes is not None:
            if num_classes == 2:
                num_classes = 1 #if 2 classes make only 1 logit for binary cross-entropy loss
            self.cls = torch.nn.Linear(config.hidden_size,num_classes)
        if dropout_prob is not None:
            self.dropout = torch.nn.Dropout(dropout_prob)

    def forward(self,input_ids,attention_mask,post_op=None):
         last_hidden_states,pooled_output = self.bert(
                    input_ids,
                    attention_mask=attention_mask).to_tuple()
         if post_op=='mean': #meaned last hidden output (batch_size,768)
                return mean_pool(last_hidden_states,attention_mask)
         elif post_op=='default': #bert's pooling output (batch_size,768)
            return pooled_output
         elif post_op=='cls': #cls token (batch_size,768)
            return last_hidden_states[:,0,:]
         elif post_op==None: #last hidden output (batch_size,max_seq_len,768)
            return last_hidden_states
         elif post_op == 'predict':#make a prediction using linear layer, (batch_size,num_classes)
            if hasattr(self,'dropout'):
                pooled_output = self.dropout(pooled_output)
            logits = self.cls(pooled_output)
            if self.cls.out_features == 1:
                return logits.reshape((-1,)) #reshape from batch_size,1 --> batch_size
            return logits
         else:
            print('Invalid post_op. Must be one of: mean, default, cls, None, predict')





class MyModel(torch.nn.Module):
    def __init__(self,max_len=512, tokenizer_lower=False):
        super().__init__()
        self.max_len = max_len
        if tokenizer_lower == True:
            self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        else:
            self.tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
        self.tokenizer.model_max_length = 10**30 #to avoid annoying warnings

    def fresh_load(self,bert_files='transformers',num_classes=None,dropout_prob=None):
        self.model = MyBert.from_pretrained(bert_files,num_classes=num_classes,dropout_prob=dropout_prob)
        self.model.cuda()
        return self

    def triplet_train(self,anchor,pos,neg,epochs=4,post_op='mean',save=None,lr=3e-5, warmup=False):
        self.setup_optimizer(lr=lr)
        log('\nPerforming triplet training with {} epochs, {} post_op, and {} lr\n'.format(epochs,post_op,lr))
        criterion=torch.nn.TripletMarginLoss(margin=1.0, p=2)
        self.model.train()
        losses = []
        for e in trange(epochs):
            tr_loss=0
            tr_steps=0
            for (anchor_input_ids,anchor_mask,_),(pos_input_ids,pos_mask,_),(neg_input_ids,neg_mask,_) in zip(anchor,pos,neg):
                anchor_output = self.model(
                    anchor_input_ids.to(device),
                    anchor_mask.to(device),
                    post_op=post_op)
                pos_output = self.model(
                    pos_input_ids.to(device),
                    pos_mask.to(device),
                    post_op=post_op)
                neg_output = self.model(
                    neg_input_ids.to(device),
                    neg_mask.to(device),
                    post_op=post_op)

                self.model.zero_grad()
                
                loss = criterion(anchor_output,pos_output,neg_output)
                loss.backward()
                self.optimizer.step()
                if warmup == True:
                    self.scheduler.step()
                tr_loss+= loss.item()
                tr_steps += 1
            losses.append(tr_loss/tr_steps)
            log('\tLoss {}\n'.format(tr_loss/tr_steps))
            if save is not None:
                self.save(save.format(e))
                log('\tSaving to '+save.format(e)+'\n')

    def supervised_train(self,train_data_loader,validation_data_loader,epochs=4,save=None,lr=3e-5,warmup=False):
        self.setup_optimizer(lr=lr,warmup=warmup,epochs=epochs,train_data_loader=train_data_loader)
        if hasattr(self.model,'dropout'):
            dropout = self.model.dropout.p
        else:
            dropout=None
        log('\nPerforming supervised training with {} epochs, {} dropout, {} lr, and {} warmup\n'.format(epochs,dropout,lr,warmup))
        if self.model.cls.out_features == 1:
            criterion = torch.nn.BCEWithLogitsLoss()
        else:
            criterion = torch.nn.CrossEntropyLoss()
        losses = []
        accuracy = []
        macro_f1 = []
        for e in trange(epochs):
            self.model.train()
            tr_loss=0
            tr_steps=0
            for input_id,mask,label in train_data_loader:
                logits = self.model(input_id.to(device),attention_mask=mask.to(device),post_op='predict')
                loss = criterion(logits,label.to(device))
                losses.append(loss.item())

                self.model.zero_grad()
                loss.backward()
                self.optimizer.step()
                if warmup == True:
                    self.scheduler.step()
                tr_loss += loss.item()
                tr_steps+=1
            log("\tLoss: {}\n".format(tr_loss/tr_steps))
            if save is not None:
                self.save(save.format(e))
                log('\tSaving file to '+save.format(e)+'\n')
            acc, f1 = self.evaluate(validation_data_loader)
            log('\tAcc {} and macro f1 {}\n'.format(acc, f1))
        return losses,accuracy,macro_f1

    def pred(self,data_loader,post_op='mean',cat=True):
        pred=[]
        self.model.eval()
        with torch.no_grad():
            for input_ids,attention_mask,_ in data_loader:
                logits = self.model(
                    input_ids.to(device),
                    attention_mask.to(device),
                    post_op=post_op)
                if post_op=='predict':
                    if self.model.cls.out_features == 1:
                        y_pred = torch.where(logits>=0.5, 1, 0).tolist()
                    else:
                        y_pred = [np.argmax(logits).item() for logits in logits.to('cpu')]
                    pred.append(y_pred)
                else:
                    pred.append(logits.to('cpu'))

        if cat==False:
            return pred
        else:
            return np.concatenate(pred,axis=0)

    def evaluate(self, data_loader):
        pred=[]
        y_true = []
        self.model.eval()
        with torch.no_grad():
            for input_ids,attention_mask,labels in data_loader:
                logits = self.model(
                    input_ids.to(device),
                    attention_mask.to(device),
                    post_op='predict')
                if self.model.cls.out_features == 1:
                    y_pred = torch.where(logits>=0.5, 1, 0).tolist()
                else:
                    y_pred = [np.argmax(logits).item() for logits in logits.to('cpu')]
                pred.append(y_pred)
                y_true.append(labels)

        y_pred = np.concatenate(pred,axis=0)
        y_true = np.concatenate(y_true, axis=0)
        return accuracy_score(y_true,y_pred), f1_score(y_true,y_pred,average='macro')


    def triplet_train_collection(self,c,batch_size=8,epochs=4, headline_emb=True, post_op='default',save=None,lr=3e-5, warmup=False):
        if len(c)%2==1:
            c=c[:-1] #assure even number

        if headline_emb == True: #if  doing headline emb use double title
            anchor = [a.text() for a in c]
            pos = [a.title for a in c]
            neg = list(reversed([a.title for a in c]))

        if headline_emb == False: #if not doing headline emb use double text
            anchor = [a.title for a in c]   
            pos = [a.text() for a in c]
            neg = list(reversed([a.text() for a in c]))

        anchor_loader = self.preprocess(anchor,batch_size=batch_size)
        pos_loader = self.preprocess(pos,batch_size=batch_size)
        neg_loader = self.preprocess(neg,batch_size=batch_size)
        self.triplet_train(anchor_loader,pos_loader,neg_loader,epochs=epochs,post_op=post_op,save=save,lr=lr, warmup=warmup)

    def supervised_train_data(self,xtr,ytr,xval,yval,batch_size=8,epochs=4,save=None,lr=3e-5,warmup=False):
        train_data_loader = self.preprocess(xtr,ytr,batch_size=batch_size)
        validation_data_loader = self.preprocess(xval,yval,batch_size=batch_size)
        return self.supervised_train(train_data_loader,validation_data_loader,epochs=epochs,save=save,lr=lr,warmup=warmup)


    def setup_optimizer(self,warmup=False,lr=3e-5,epochs=None,train_data_loader=None,warmup_percent=0.1):
        param_optimizer = list(self.model.named_parameters())
        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
    {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
     'weight_decay_rate': 0.01},
    {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)],
     'weight_decay_rate': 0.0}]
        if warmup == False:
            self.optimizer = AdamW(optimizer_grouped_parameters,lr=lr)
        elif warmup == True:
            self.optimizer = AdamW(optimizer_grouped_parameters,lr=lr)
            total_steps = len(train_data_loader)*epochs
            self.scheduler = get_linear_schedule_with_warmup(self.optimizer,num_warmup_steps=total_steps*warmup_percent,num_training_steps=total_steps)




    def preprocess(self,x,y=None,batch_size=32):
        if y is not None:
            if self.model.cls.out_features == 1: #BCE loss expects float
                labels = torch.tensor(y).float()
            else:
                labels = torch.tensor(y).long()
        else:
            labels = torch.tensor([np.nan]*len(x))
        input_ids =[self.tokenizer.encode(text,add_special_tokens=True,padding=False,truncation=False,verbose=False) for text in x]
        input_ids = torch.tensor(pad_sequences(input_ids,maxlen=self.max_len)).to(torch.int64)
        attention_masks=[]
        for seq in input_ids:
            seq_mask = [float(i>0) for i in seq]
            attention_masks.append(seq_mask)
            
        masks = torch.tensor(attention_masks)
        data = TensorDataset(input_ids, masks, labels)
        dataloader = DataLoader(data, batch_size=batch_size)
        return dataloader


    def save(self,path):
        state = self.state_dict()
        torch.save(state,path)

    def load(self,path,strict=True,extra_args={}):
        state=torch.load(path)
        state.update(extra_args)
        self.load_state_dict(state,strict=strict)

    def from_pretrained(self,path,bert_files='transformers',dropout_prob=None,num_classes=None,strict=True,extra_args={}):
        self.model = MyBert.from_pretrained(bert_files,dropout_prob=dropout_prob,num_classes=num_classes)
        self.load(path,strict=strict,extra_args=extra_args)
        self.model.cuda()
        return self



def pad_sequences(inputs,maxlen):
    padded=[]
    for item in inputs:
        if len(item)>maxlen:
            padded.append(item[:maxlen])
        elif len(item)<maxlen:
            padded.append(item+(maxlen-len(item))*[0]) #add 0's equal to the difference between maxlen and inputs
        else:
            padded.append(item)
    return padded

def mean_pool(token_embeddings,attention_mask):
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    sum_emb = torch.sum(token_embeddings * input_mask_expanded, 1)
    sum_mask = input_mask_expanded.sum(1)
    return sum_emb/sum_mask
