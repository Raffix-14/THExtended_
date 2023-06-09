import spacy
from datasets import Dataset
import pandas as pd
from math import *
from collections import defaultdict
import seaborn as sns
import matplotlib.pyplot as plt
import gc
import time
from tqdm import tqdm
from evaluate import load
import os
import sys
import torch
import random
import logging
import traceback
import numpy as np
from os.path import join
from rouge import Rouge
import difflib
from multiprocessing import cpu_count
import statistics
from datasets import load_dataset, load_from_disk
from datasets.utils import logging as loggingDatasets
from transformers.utils import logging as loggingTransformer
from transformers import TrainerCallback
from sentence_transformers import util as util_st
from nltk import ngrams


def clean_dataset(dataset, num_samples, seed=42):
    nlp = spacy.load("en_core_web_lg")
    # Shuffle the dataset, so you can be sure you're not selecting the first contiguous data points
    ds = dataset.shuffle(seed=seed)
    # Create the cleaned ds and init the index
    cleaned_ds = []
    i = -1
    # Thresholds
    before_th = 40

    # Start looping on the shuffled ds and collect the good, cleaned samples
    while len(cleaned_ds) < num_samples:

        # Select the data point
        i += 1
        data_point = ds[i]
        article = data_point["article"]
        highlights = nlp(data_point["highlights"])

        cleaned_high = ""
        n_highlight = 0
        # For each hl, check whether to discard it
        for current_h in highlights.sents:
            if len(current_h.text.split(" ")) > 3 and any(char.isalpha() for char in current_h.text):
                n_highlight += 1
                if cleaned_high != "":
                    cleaned_high += "\n"
                cleaned_high += current_h.text.replace("\n", "").strip()

        if 3 <= n_highlight <= 5:  # If the highlights are more than 3 and less than 5 is good.
            data_point['highlights'] = cleaned_high  # Updating the 'highlights' field of the considered data_point.
        else:
            continue

        # Check if "--" is in the article
        if "--" in article:
            # Check the length of the text before the "--"; if it's less than a threshold, remove it from the article
            text_before, text_after = article.split("--", 1)
            if len(text_before) <= before_th:
                article = text_after
                data_point["article"] = article
        # Now do the same with "(CNN)"
        if "(CNN)" in article:
            text_before, text_after = article.split("(CNN)", 1)
            if len(text_before) <= before_th:
                article = text_after
                data_point["article"] = article

        # ADDITIONAL CLEANING: SOMETIMES THE ARTICLES HAVE A BEGINNING LIKE word . word word . word .
        splits = article.split(".")
        for j, split in enumerate(splits):
            words = [word for word in split.split() if not any(char.isdigit() for char in word)]
            if len(words) <= 2:
                continue
            else:
                article = ".".join(splits[j:]).strip()
                data_point['article'] = article
                break

        ###########################################################################################

        # Finally, filter out articles that are too short
        if len(article) < 300:
            continue

        # Append the data point to the cleaned ds
        cleaned_ds.append(data_point)

    return Dataset.from_pandas(pd.DataFrame(data=cleaned_ds))


def prepare_dataset(dataset_path=None,
                    num_train_examples=3000,
                    num_val_examples=500,
                    num_test_examples=500,
                    save_flag=0,
                    save_dir=None,
                    seed=42):
    from DataParser import DataParser
    train_dataset, val_dataset, test_dataset = None, None, None

    # Check if a path was provided and if a file exists at the path
    if dataset_path is not None and os.path.isdir(dataset_path):
        for data_type in ["train", "validation", "test"]:
            data_path = os.path.join(dataset_path, data_type)
            try:
                logging.info(f"Loading {data_type} dataset from {data_path}")
                if data_type == "train":
                    train_dataset = load_from_disk(data_path)
                elif data_type == "validation":
                    val_dataset = load_from_disk(data_path)
                elif data_type == "test":
                    test_dataset = load_from_disk(data_path)
            except FileNotFoundError:
                logging.info(f"No dataset {data_type.upper()} split found at {data_path}. Loading default.")

    # If save_flag is set and a path is provided, save the dataset
    if save_flag == 1:
        if dataset_path is None:
            dataset_path = os.path.join(save_dir, "dataset")
        if not os.path.exists(dataset_path):
            logging.debug("Creating folder {dataset_path} to store the dataset splits")
            os.makedirs(dataset_path, exist_ok=True)

    if train_dataset is None or val_dataset is None or test_dataset is None:
        logging.info("Loading CNN DailyMail dataset from Hugging Face Hub")
        raw_dataset = load_dataset("cnn_dailymail", "3.0.0")

        # Apply cleaning and parsing steps to training dataset
        if train_dataset is None:
            logging.info("Cleaning and parsing TRAIN split")
            cleaned_train_dataset = clean_dataset(raw_dataset['train'], num_train_examples, seed=seed)
            parser = DataParser(dataset=cleaned_train_dataset)
            train_dataset = parser()
            if save_flag:
                data_path = os.path.join(dataset_path, 'train')
                logging.info(f"Saving dataset TRAIN split to {data_path}")
                train_dataset.save_to_disk(data_path)

        # Apply cleaning and parsing steps to validation dataset
        if val_dataset is None:
            logging.info("Cleaning and parsing VALIDATION split")
            cleaned_val_dataset = clean_dataset(raw_dataset['validation'], num_val_examples, seed=seed)
            parser = DataParser(dataset=cleaned_val_dataset)
            val_dataset = parser()
            if save_flag:
                data_path = os.path.join(dataset_path, 'validation')
                logging.info(f"Saving dataset VALIDATION split to {data_path}")
                val_dataset.save_to_disk(data_path)

        # Apply cleaning and parsing steps to test dataset
        if test_dataset is None:
            logging.info("Cleaning and parsing TEST split")
            cleaned_test_dataset = clean_dataset(raw_dataset['test'], num_test_examples, seed=seed)
            parser = DataParser(dataset=cleaned_test_dataset, is_test=True)
            test_dataset = parser()
            if save_flag:
                data_path = os.path.join(dataset_path, 'test')
                logging.info(f"Saving dataset TEST split to {data_path}")
                test_dataset.save_to_disk(data_path)
    return train_dataset, val_dataset, test_dataset


def compute_rouges(sentences, references, aggregation='max', is_test=False):
    
    rouges = []
    rouge_model = Rouge()

    if aggregation == 'max':
        aggregate_f = lambda x: max(x)
    elif aggregation == 'average':
        aggregate_f = lambda x: sum(x) / len(x)
    elif aggregation == 'harmonic':
        aggregate_f = lambda x: statistics.harmonic_mean(x)
    else:
        # If an invalid value is provided for `aggregation`, a `ValueError` is raised.
        raise ValueError(f"Invalid aggregation parameter: {aggregation}")

    for sentence in sentences:
        tmp_rouges = []
        # Skip empty sentences or sentences without words
        if not sentence.strip() or not any(char.isalpha() for char in sentence):
            if is_test:
                rouges.append(
                    {
                        "rouge-1": {"f": 0.0, "p": 0.0, "r": 0.0},
                        "rouge-2": {"f": 0.0, "p": 0.0, "r": 0.0},
                        "rouge-l": {"f": 0.0, "p": 0.0, "r": 0.0}
                    })
            else:
                rouges.append(0.0)
            continue
        for reference in references:
            if is_test:
                try:
                    rouge_score = rouge_model.get_scores(sentence, reference)[0]
                except:
                    rouge_score = {
                        "rouge-1": {"f": 0.0, "p": 0.0, "r": 0.0},
                        "rouge-2": {"f": 0.0, "p": 0.0, "r": 0.0},
                        "rouge-l": {"f": 0.0, "p": 0.0, "r": 0.0}
                    }
                    logging.debug("-----------------------------ROUGE ERROR-----------------------------")
                    logging.debug(sentence)
                    logging.debug(reference)
                    logging.debug(references)
                    logging.debug("---------------------------------------------------------------------")
            else:
                try:
                    rouge_score = rouge_model.get_scores(sentence, reference)[0]["rouge-2"]['f']
                except:
                    rouge_score = 0.0
                    logging.debug("-----------------------------ROUGE ERROR-----------------------------")
                    logging.debug(sentence)
                    logging.debug(reference)
                    logging.debug(references)
                    logging.debug("---------------------------------------------------------------------")

            tmp_rouges.append(rouge_score)

        # Rouge aggregation
        if is_test:
            aggregated_rouges = aggregate_test_scores(tmp_rouges)
            rouges.append(aggregated_rouges)
        else:
            rouges.append(aggregate_f(tmp_rouges))
    return rouges


def compute_similarities(sentences, references, similarity_model=None, aggregation='max'):
    with torch.no_grad():
        embeddings1 = similarity_model.encode(sentences, convert_to_tensor=True, show_progress_bar=False)
        embeddings2 = similarity_model.encode(references, convert_to_tensor=True, show_progress_bar=False)
    cosine_scores = util_st.cos_sim(embeddings1, embeddings2)

    if aggregation == 'max':
        similarities = torch.max(cosine_scores, dim=1)[0]
    elif aggregation == 'average':
        similarities = torch.mean(cosine_scores, dim=1)
    elif aggregation == 'harmonic':
        similarities = 1 / torch.mean(torch.reciprocal(cosine_scores), dim=1)
    else:
        # If an invalid value is provided for `aggregation`, a `ValueError` is raised.
        raise ValueError(f"Invalid aggregation parameter: {aggregation}")
    return similarities.tolist()

def compute_mrr_single_doc(sents_pred, sents_gt):
    reciprocal_ranks = []

    for gt_highlight in sents_gt:
        for rank, item in enumerate(sents_pred, start=1):
            if is_similar_string(item, gt_highlight):
                reciprocal_ranks.append(1 / rank)
                break

    if reciprocal_ranks:
        return max(reciprocal_ranks)
    else:
        return 0.0

def is_similar_string(string1, string2):
    # Substring matching
    if string1 in string2 or string2 in string1:
        return True
    
    # Fuzzy matching
    ratio = difflib.SequenceMatcher(None, string1, string2).ratio()
    if ratio >= 0.8:  # Adjust the threshold as needed
        return True
    
    return False

def aggregate_test_scores(scores):
    # Initialize variables to keep track of maximum "f" and corresponding dictionaries
    max_rouge_2_f = 0.0
    best_dict = {}

    # Iterate through the list of dictionaries
    for dictionary in scores:
        rouge_2_f = dictionary["rouge-2"]["f"]
        if rouge_2_f >= max_rouge_2_f:
            best_dict = dictionary
            max_rouge_2_f = rouge_2_f
    return best_dict


def get_scores(sentences, context, model, tokenizer):
    context_list = [context] * len(sentences)
    inputs = tokenizer(sentences, context_list, truncation="only_second", padding="max_length", return_tensors="pt")
    # Print each article separately
    # for x in inputs["input_ids"]:
    # logging.debug(tokenizer.decode(x))
    # logging.debug("---------------------------------------------------------------------")
    inputs.to(torch.device("cuda:0"))
    with torch.no_grad():
        outputs = model(**inputs)
    scores = outputs.logits.squeeze().tolist()
    sorted_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    sorted_sentences = [sentences[i] for i in sorted_indices]
    sorted_scores = [scores[i] for i in sorted_indices]

    return sorted_sentences, sorted_scores


def trigram_blocking(sentences):
    summary = []
    trigrams_summary = set()

    for sentence in sentences:
        sentence_trigrams = set(ngrams(sentence.lower().split(), 3))
        if not trigrams_summary.intersection(sentence_trigrams):
            summary.append(sentence)
            trigrams_summary.update(sentence_trigrams)

    return summary

class Explorer:

    def __init__(self, dataset):
        self.nlp = spacy.load('en_core_web_lg')
        self.ds = dataset
        self.bertscore = load("bertscore")

    @staticmethod
    def split_sentence_article(nlp, text):
        doc = nlp(text)
        sentences = []
        for sent in doc.sents:
            sentence = sent.text.replace("\n", "")
            sentence = sentence.strip()
            sentences.append(sentence)
        return sentences

    @staticmethod
    def create_sections(sentences):
        # Compute the index ranges for each bucket
        n = len(sentences)
        cut1 = ceil(n / 3)
        cut2 = ceil(cut1 + ((n - cut1) / 2))
        # Create the sections
        section1 = sentences[:cut1]
        section2 = sentences[cut1:cut2]
        section3 = sentences[cut2:]
        return [section1, section2, section3]

    def compute_similarity(self, article, section):
        return self.bertscore.compute(predictions=[section], references=[article],
                                      model_type="allenai/longformer-base-4096")

    @staticmethod
    def plot_similarities(similarities):

        # Boxplots
        # Convert the dictionary to a DataFrame
        df = pd.DataFrame(dict([(k, pd.Series(v)) for k, v in similarities.items()]))
        # Melt the DataFrame to a long format
        df = df.melt(var_name='Section', value_name='Similarity')
        # Set the theme
        sns.set_theme(style="whitegrid")
        # Create the boxplots
        plt.figure(figsize=(10, 7))
        sns.boxplot(x='Section', y='Similarity', data=df, palette="Set3")
        plt.title('Similarities by Section', fontsize=20)
        plt.xlabel('Section', fontsize=15)
        plt.ylabel('Similarity', fontsize=15)
        plt.ylim(0, 1)
        plt.show()

        # Heatmap
        # Convert the dictionary to a DataFrame and transpose it
        df = pd.DataFrame(similarities)

        sns.set_theme()

        plt.figure(figsize=(10, 7))
        sns.heatmap(df, annot=False, cmap="YlGnBu")
        plt.title('Similarities by Section and Article', fontsize=20)
        plt.ylabel('Article', fontsize=15)
        plt.xlabel('Section', fontsize=15)
        plt.show()

    def explore(self):
        # For each data point
        start = time.time()
        similarities = defaultdict(list)
        for data_point in tqdm(self.ds, desc=" Iterating cleaned dataset"):
            # Take its article
            article = data_point["article"]
            # Split it in sentences
            sentences = self.split_sentence_article(self.nlp, article)
            # Divide the sentences in 3 buckets (start, middle, finish)
            sections = self.create_sections(sentences)
            # For each section, compute the BertSimilarity with the whole article
            for i, section in enumerate(sections, start=1):
                section_text = ' '.join(section)
                similarity = self.compute_similarity(article, section_text)
                similarities[i].append(similarity['f1'][0])
                # Cleaning up
                del section_text, similarity
                torch.cuda.empty_cache()
                gc.collect()
        end = time.time()
        print(f"Done i {end - start} seconds")
        self.plot_similarities(similarities)


def setup_logging(save_dir, console="debug", info_filename="info.log", debug_filename="debug.log"):
    """Set up logging files and console output.
    Creates one file for INFO logs and one for DEBUG logs.
    Args:
        save_dir (str): creates the folder where to save the files.
        console (str):
            if == "debug" prints on console debug messages and higher
            if == "info"  prints on console info messages and higher
            if == None does not use console (useful when a logger has already been set)
        info_filename (str): the name of the info file. if None, don't create info file
        debug_filename (str): the name of the debug file. if None, don't create debug file
    """
    if os.path.exists(save_dir):
        raise FileExistsError(f"{save_dir} already exists!")
    os.makedirs(save_dir, exist_ok=True)
    # print(logging.Logger.manager.loggerDict.keys()) # to check which loggers are in use
    base_formatter = logging.Formatter('%(asctime)s   %(message)s', "%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger('')
    logger.setLevel(logging.DEBUG)

    loggingTransformer.set_verbosity_debug()
    loggerTransformer = loggingTransformer.get_logger("transformers")

    loggingDatasets.set_verbosity_debug()
    loggerDatasets = loggingDatasets.get_logger("datasets")

    if info_filename is not None:
        info_file_handler = logging.FileHandler(join(save_dir, info_filename))
        info_file_handler.setLevel(logging.INFO)
        info_file_handler.setFormatter(base_formatter)
        logger.addHandler(info_file_handler)
        loggerTransformer.addHandler(info_file_handler)
        loggerDatasets.addHandler(info_file_handler)

    if debug_filename is not None:
        debug_file_handler = logging.FileHandler(join(save_dir, debug_filename))
        debug_file_handler.setLevel(logging.DEBUG)
        debug_file_handler.setFormatter(base_formatter)
        logger.addHandler(debug_file_handler)
        loggerTransformer.addHandler(debug_file_handler)
        loggerDatasets.addHandler(debug_file_handler)

    if console is not None:
        console_handler = logging.StreamHandler()
        if console == "debug":
            console_handler.setLevel(logging.DEBUG)
            loggingTransformer.set_verbosity_debug()
            loggingDatasets.set_verbosity_debug()
            loggingTransformer.disable_default_handler()

        if console == "info":
            console_handler.setLevel(logging.INFO)
            loggingTransformer.set_verbosity_info()
            loggingDatasets.set_verbosity_info()
            loggingTransformer.disable_default_handler()

        console_handler.setFormatter(base_formatter)
        logger.addHandler(console_handler)
        loggerTransformer.addHandler(console_handler)

    def exception_handler(type_, value, tb):
        logger.info("\n" + "".join(traceback.format_exception(type_, value, tb)))

    sys.excepthook = exception_handler


class LogCallback(TrainerCallback):
    def on_evaluate(self, args, state, control, **kwargs):
        final_str = "##### EVALUATION STEP RESULTS #####\n"
        for step in state.log_history:
            for k, v in step.items():
                final_str += str(k) + ': ' + str(v) + '\n'
            final_str += "---------------------------------\n"
        logging.debug(final_str)
        return


def make_deterministic(seed=0):
    """Make results deterministic. If seed == -1, do not make deterministic.
    Running the script in a deterministic way might slow it down.
    """
    if seed == -1:
        return
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
