import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__),'../'))
from core.monitoring.data_collector import Online_Collector
from core.data_engineering import data_selection  

import argparse
from azureml.core import Workspace
import pandas as pd
import os
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.helpers import dataframe_from_result_table
import json
from azureml.core import Dataset
from azureml.core.authentication import ServicePrincipalAuthentication
from azureml.data import DataType
from sklearn.model_selection import train_test_split
import datetime, time
def select_data(strategy,tenant_id,client_id,client_secret,cluster_uri,database_name, scoring_table,all_data_table_name, model_name, examples_limit, prob_limit):
    all_labeled_examples = get_all_labeled_data(tenant_id,client_id,client_secret,cluster_uri,database_name, all_data_table_name)
    if strategy == "SMALLEST_MARGIN_UNCERTAINTY":
        examples = data_selection.smallest_margin_uncertainty(tenant_id,client_id,client_secret,cluster_uri,database_name, scoring_table, model_name, limit=examples_limit, prob_limit=prob_limit)
    elif strategy == "ENTROPHY_SAMPLING":
        examples = data_selection.entrophy_sampling(tenant_id,client_id,client_secret,cluster_uri,database_name, scoring_table, model_name, limit=examples_limit, prob_limit=prob_limit)
    elif strategy == "LEAST_CONFIDENCE":
        examples = data_selection.least_confidence(tenant_id,client_id,client_secret,cluster_uri,database_name, scoring_table, model_name, limit=examples_limit, prob_limit=prob_limit)

    else: #Random sampling
        examples = data_selection.random_sampling(tenant_id,client_id,client_secret,cluster_uri,database_name, scoring_table, model_name, limit=examples_limit)

    labeled_examples = all_labeled_examples.merge(examples, on = "file_path")[['file_path', 'label']]

    return labeled_examples
def get_all_labeled_data(tenant_id,client_id,client_secret,cluster_uri,db, all_data_table_name):
    KCSB_DATA = KustoConnectionStringBuilder.with_aad_application_key_authentication(cluster_uri, client_id, client_secret, tenant_id)
    client = KustoClient(KCSB_DATA)
    query= f"""
    {all_data_table_name}
    """
    response = client.execute(db, query)

    return dataframe_from_result_table(response.primary_results[0])[['file_path', 'label']]

def get_previous_train_data(tenant_id,client_id,client_secret,cluster_uri,db, train_data_table_name,train_dataset_name, max_records=1000):
    KCSB_DATA = KustoConnectionStringBuilder.with_aad_application_key_authentication(cluster_uri, client_id, client_secret, tenant_id)
    client = KustoClient(KCSB_DATA)
    query= f"""
    {train_data_table_name}| where dataset_name == '{train_dataset_name}'| project file_path, label
    """
    response = client.execute(db, query)
    result = dataframe_from_result_table(response.primary_results[0])
    # if result.shape[0]>max_records:
    #     result = result.sample(max_records)
    return result

def create_aml_label_dataset(ws,datastore, target_path, input_ds, dataset_name):
    # sample json line dictionary
    json_line_sample = {
        "image_url": "AmlDatastore://"
        + "some_ds"
        + "/",
        "label": "",
    }
    try:
        new_version = ws.datasets[dataset_name].version+1
    except:
        new_version = 1
    
    annotations_file =dataset_name+f"_v_{new_version}"+".jsonl"


    with open(annotations_file, "w") as train_f:
        for _, row in input_ds.iterrows():
            file_path ="/".join(row["file_path"].split("/")[5:])
            json_line = dict(json_line_sample)
            json_line["image_url"] = "AmlDatastore://"+datastore.name+"/"+file_path
            json_line["label"] = row['label']
            train_f.write(json.dumps(json_line) + "\n")
    datastore.upload_files(files=[annotations_file], target_path=target_path,overwrite=True)


    dataset = Dataset.Tabular.from_json_lines_files(
        path=datastore.path(f"{target_path}/{annotations_file}"),
        set_column_types={"image_url": DataType.to_stream(datastore.workspace)},
    )
    #the goal is to use the same name but with new version, each version refer to the label dataset name 
    dataset = dataset.register(
        workspace=datastore.workspace, name=dataset_name,create_new_version=True, description
        =dataset_name
    )
    print("register  ", dataset_name)
    return dataset

def create_init_train_ds(ws,datastore,train_dataset_name,jsonl_target_path, strategy,train_data_table_name, size,tenant_id,client_id,client_secret,cluster_uri,db, all_data_table_name, random_state=101):
    all_labeled_data = get_all_labeled_data(tenant_id,client_id,client_secret,cluster_uri,db, all_data_table_name)
    train_ds, _ = train_test_split(all_labeled_data, test_size = 1-(size/all_labeled_data.shape[0]),random_state=random_state, stratify=all_labeled_data['label'])
    print("train_ds size ", train_ds.shape)
    print("label distribution of trainset ", train_ds.groupby("label").count())
    ts = datetime.datetime.now()
    train_aml_dataset= create_aml_label_dataset(ws,datastore, jsonl_target_path,  train_ds,train_dataset_name)
    train_ds['timestamp'] =ts
    train_ds['dataset_name'] =train_aml_dataset.name
    train_ds['strategy'] =strategy
    sample_data = train_ds.head(10)
    collector = Online_Collector(tenant_id, client_id,client_secret,cluster_uri,db,train_data_table_name, sample_data)
    t=0
    while(t<10):
        try:
            collector.batch_collect(train_ds)
            break
        except:
            #tables are not ready, retry
            time.sleep(20)
        t+=1



# define functions
def main(args):
    secret = os.environ.get("SP_SECRET")
    client_id = os.environ.get("SP_ID")
    f=open(args.param_file)
    params =json.load(f)
    tenant_id = params["tenant_id"]
    workspace_name = params['workspace_name']
    subscription_id = params['subscription_id']
    resource_group = params['resource_group']
    sp = ServicePrincipalAuthentication(tenant_id=tenant_id, service_principal_id=client_id,service_principal_password=secret)
    ws = Workspace.get(workspace_name, subscription_id=subscription_id, resource_group=resource_group, auth=sp)    
    kv=ws.get_default_keyvault()

    database_name=params["database_name"]
    client_secret = kv.get_secret(client_id)
    cluster_uri = params["cluster_uri"]
    base_path =params["base_path"]
    all_data_table_name=params["all_data_table_name"]
    datastore_name =params["datastore_name"]
    scoring_table= params["scoring_table"]
    jsonl_target_path= params["jsonl_target_path"]

    train_dataset_name= params["train_dataset"]
    strategy = params['strategy']
    size = params['initial_train_size']
    train_data_table_name=params["train_data_table_name"]
    model_name = params['model_name']
    max_prev_records= params['max_prev_records']
    datastore = ws.datastores[datastore_name]

    #check if this is initial run, then create init dataset only
    try:
        ws.datasets[train_dataset_name] #dataset exist, then this is not the first run.
    except:
        print(f"dataset {train_dataset_name} does not exist, this is initial run, go on creating train dataset ")
        create_init_train_ds(ws,datastore,train_dataset_name,jsonl_target_path, strategy,train_data_table_name,size,tenant_id,client_id,client_secret,cluster_uri,database_name, all_data_table_name, random_state=101)
        return

    client_secret = kv.get_secret(client_id)
    new_examples = select_data(strategy,tenant_id,client_id,client_secret,cluster_uri,database_name, scoring_table,all_data_table_name,model_name, examples_limit=50, prob_limit=10)
    previous_train_dataset =get_previous_train_data(tenant_id,client_id,client_secret,cluster_uri,database_name, train_data_table_name,train_dataset_name,max_prev_records)
    print("net dataset size ", new_examples.shape)
    examples = pd.concat([new_examples[['file_path', 'label']],previous_train_dataset])
    ts = datetime.datetime.now()

    print("whole new train dataset size ", examples.shape)

    train_aml_dataset= create_aml_label_dataset(ws,datastore, jsonl_target_path,  examples,train_dataset_name)
    new_examples['timestamp'] =ts
    new_examples['dataset_name'] =train_dataset_name
    new_examples['strategy'] =strategy
    sample_data = new_examples.head(10)
    collector = Online_Collector(tenant_id, client_id,client_secret,cluster_uri,database_name,train_data_table_name, sample_data)
    collector.batch_collect(new_examples)


def parse_args():
    # setup arg parser
    parser = argparse.ArgumentParser()
    parser.add_argument("--param_file", type=str)

    # add arguments

    # parse args
    args = parser.parse_args()

    # return args
    return args

if __name__ == "__main__":
    # parse args
    args= parse_args()
    main(args)
