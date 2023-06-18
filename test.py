import ArgsParser
import logging
import os
from datetime import datetime
from utils import setup_logging, make_deterministic, prepare_dataset, get_scores, compute_labels
import torch
import numpy as np
from multiprocessing import cpu_count
from transformers import AutoTokenizer, AutoModelForSequenceClassification, TrainingArguments, Trainer
from sentence_transformers import SentenceTransformer

# If the GPU is based on the nvdia Ampere architecture uncomment this line as it speed-up training up to 3x reducing memory footprint
# torch.backends.cuda.matmul.allow_tf32 = True

def evaluate_model(dataset, model, tokenizer, num_highlight=3):
    current_context = None
    current_article_sentences = []
    current_highlight = None
    rouges = []
    similarities = []

    for example in dataset:
        sentence = example['sentence']
        context = example['context']
        highlights = example['highlights'].split("\n")

        # Check if context has changed
        if context != current_context:
            # Process previous article
            if current_context is not None:
                ranked_sents, ranked_scores = get_scores(current_article_sentences, current_context, model, tokenizer)
                rouge_dict, similarity = evaluate_article(ranked_sents[:num_highlight], highlights)
                rouges.append(rouge_dict)
                similarities.append(similarity)

            # Start a new article
            current_context = context
            current_highlight = highlights
            current_article_sentences = []

        # Append sentence to current article
        current_article_sentences.append(sentence)

    # Process the last article
    if current_context is not None:
        ranked_sents, ranked_scores = get_scores(current_article_sentences, current_context, model, tokenizer)
        rouge_dict, similarity = evaluate_article(ranked_sents[:num_highlight], current_highlight)
        rouges.append(rouge_dict)
        similarities.append(similarity)

    return compute_avg_dict(rouges), np.mean(similarities)


def evaluate_article(highlights_pred, highlights_gt):
    """
    Compute the ROUGE score between the predicted highlights and the ground truth highlights
    Args:
        highlights_pred: highlights predicted by the model
        highlights_gt: ground truth highlights

    Returns: a dictionary containing the ROUGE scores

    """
    rouges = []
    semantic_similarities = []
    for h in highlights_pred:
        rouge_score, similarity_score = compute_labels(h, highlights_gt, is_test=True, \
                                                           similarity_model = similarity_model)
        rouges.append(rouge_score)
        semantic_similarities.append(similarity_score)

    return compute_avg_dict(rouges), np.mean(semantic_similarities)


def compute_avg_dict(dict_list):
    # Iterate through the list of dictionaries
    avg_dict = {}
    keys = ["rouge-1", "rouge-2", "rouge-l"]
    metrics = ["f", "p", "r"]

    for key in keys:
        avg_dict[key] = {}
        for metric in metrics:
            avg_dict[key][metric] = sum(dictionary[key][metric] for dictionary in dict_list) / len(dict_list)

    return avg_dict

def main():
    
    # Initial setup: parser, logging...
    args = ArgsParser.parse_arguments()
    start_time = datetime.now()
    args.output_dir = os.path.join(args.output_dir, start_time.strftime('%Y-%m-%d_%H-%M-%S'))

    setup_logging(os.path.join(args.output_dir, "logs"), "info")
    make_deterministic(args.seed)
    logging.info(f"Arguments: {args}")
    logging.info(f"The outputs are being saved in {args.output_dir}")
    logging.info(f"Using {torch.cuda.device_count()} GPUs and {cpu_count()} CPUs")
    
    model_name = args.model_name_or_path
    
    global similarity_model
    
    logging.info("\n|-------------------------------------------------------------------------------------------|")
    logging.info(f"##### DOWNLOADING MODEL {model_name} #####")
    model = AutoModelForSequenceClassification.from_pretrained(model_name, num_labels=1)
    similarity_model = SentenceTransformer("all-MiniLM-L6-v2") 
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True)
    model.to(torch.device("cuda:0"))
    model.eval()
    logging.info("\n|-------------------------------------------------------------------------------------------|")

    logging.info("##### PREPARING TEST DATASETS #####")
    _, _, dataset_test = prepare_dataset(args.dataset_path,
                                         args.num_train_examples,
                                         args.num_val_examples,
                                         args.num_test_examples,
                                         args.save_dataset_on_disk,
                                         args.output_dir,
                                         args.seed)
    logging.info("\n|-------------------------------------------------------------------------------------------|")

    logging.debug("##### EXAMPLE DATAPOINT #####")
    logging.debug(dataset_test[0])
    logging.debug("|-------------------------------------------------------------------------------------------|")

    logging.info("##### EVALUATING MODEL #####")
    results = evaluate_model(dataset_test, model, tokenizer)
    for key, value in results[0].items():
        logging.info(f"{key}: {value}")
    logging.info(f"Mean semantic similarity: {results[1]}")
    logging.info("\n|-------------------------------------------------------------------------------------------|")


if __name__ == '__main__':
    main()
