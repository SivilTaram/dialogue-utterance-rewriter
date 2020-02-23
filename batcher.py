# -*- coding: utf-8 -*-
# todo: query可能是"无"
"""This file contains code to process data into batches"""

import queue
from random import shuffle
from threading import Thread
import time
import numpy as np
import tensorflow as tf
from data import *


# from preprocessing import split_text_with_whitespace


class Example(object):
    """Class representing a train/val/test example for text summarization."""

    def __init__(self, context, summarization, query, vocab, hps):
        """Initializes the Example, 
        performing tokenization and truncation to produce the encoder, decoder and target sequences, which are stored in self.

        Args:
            article: source text; a string. each token is separated by a single space.
            summarization_sentences: list of strings, one per summarization sentence. 
            In each sentence, each token is separated by a single space.
            vocab: Vocabulary object
            hps: hyperparameters
        """
        self.hps = hps

        start_decoding = vocab.word2id(MARK_GO)
        stop_decoding = vocab.word2id(MARK_EOS)

        context_words = split_text_with_whitespace(context) + [MARK_EOS]
        # if len(context_words) > hps.max_enc_steps:
        #     context_words = context_words[:hps.max_enc_steps]
        self.enc_len = len(context_words)
        self.enc_input = [vocab.word2id(w) for w in context_words]

        query_words = split_text_with_whitespace(query) + [MARK_EOS]  # + ' '
        self.query_len = len(query_words)
        self.query_input = [vocab.word2id(w) for w in query_words]

        summarization_words = split_text_with_whitespace(summarization)
        summarization_ids = [vocab.word2id(w) for w in summarization_words]

        # Get the decoder input sequence and target sequence
        # todo: 注意max_dec_steps
        self.dec_input, self.target = self.get_dec_inp_targ_seqs(
            summarization_ids, hps.max_dec_steps, start_decoding,
            stop_decoding)
        self.dec_len = len(self.dec_input)

        # Store a version of the enc_input where in-article OOVs are represented by their temporary OOV id; 
        # also store the in-article OOVs words themselves
        self.enc_input_extend_vocab, self.oovs = context2ids(context_words, vocab)
        self.query_input_extend_vocab, self.oovs = query2ids(query_words, vocab, self.oovs)

        # Get a verison of the reference summary where in-article OOVs are represented by their temporary article OOV id
        abs_ids_extend_vocab = summarization2ids(summarization_words, vocab, self.oovs)

        # Overwrite decoder target sequence so it uses the temp article OOV ids
        # todo: 为什么 decoder input不用重写扩展
        _, self.target = self.get_dec_inp_targ_seqs(
            abs_ids_extend_vocab, hps.max_dec_steps, start_decoding,
            stop_decoding)

        # Store the original strings
        self.original_context = context
        self.original_summarization = summarization
        self.original_query = query

    def get_dec_inp_targ_seqs(self, sequence, max_len, start_id, stop_id):
        """Given the reference summary as a sequence of tokens, 
        return the input sequence for the decoder, and the target sequence which we will use to calculate loss. 
        The sequence will be truncated if it is longer than max_len. 
        The input sequence must start with the start_id and the target sequence must end with the stop_id (but not if it's been truncated).

        Args:
            sequence: List of ids (integers)
            max_len: integer
            start_id: integer
            stop_id: integer

        Returns:
            inp: sequence length <=max_len starting with start_id
            target: sequence same length as input, ending with stop_id only if there was no truncation
        """
        inp = [start_id] + sequence[:]
        target = sequence[:]
        if len(inp) > max_len:  # truncate
            inp = inp[:max_len]
            target = target[:max_len]  # no end_token
        else:  # no truncation
            target.append(stop_id)  # end token
        assert len(inp) == len(target)
        return inp, target

    def pad_decoder_inp_targ(self, max_len, pad_id):
        """Pad decoder input and target sequences with pad_id up to max_len."""
        self.dec_input += [pad_id] * (max_len - len(self.dec_input))
        self.target += [pad_id] * (max_len - len(self.target))

    def pad_encoder_input(self, max_len, pad_id):
        """Pad the encoder input sequence with pad_id up to max_len."""
        # while len(self.enc_input) < max_len:
        self.enc_input += [pad_id] * (max_len - len(self.enc_input))
        self.enc_input_extend_vocab += [pad_id] * (
                max_len - len(self.enc_input_extend_vocab))

    def pad_query_input(self, max_len, pad_id):
        self.query_input += [pad_id] * (max_len - len(self.query_input))
        self.query_input_extend_vocab += [pad_id] * (
                max_len - len(self.query_input_extend_vocab))


class Batch(object):
    """Class representing a minibatch of train/val/test examples for text summarization."""

    def __init__(self, example_list, hps, vocab):
        """Turns the example_list into a Batch object.

        Args:
          example_list: List of Example objects
          hps: hyperparameters
          vocab: Vocabulary object
        """
        self.pad_id = vocab.word2id(MARK_PAD)
        self.init_encoder_seq(example_list, hps)
        self.init_decoder_seq(example_list, hps)
        self.store_orig_strings(example_list)  # store the original strings

    def init_encoder_seq(self, example_list, hps):
        """Initializes the following:
        self.enc_batch:
            numpy array of shape (batch_size, <=max_enc_steps) containing integer ids (all OOVs represented by UNK id), 
            padded to length of longest sequence in the batch
        self.enc_lens:
            numpy array of shape (batch_size) containing integers. The (truncated) length of each encoder input sequence (pre-padding).
        self.enc_padding_mask:
            numpy array of shape (batch_size, <=max_enc_steps), containing 1s and 0s. 1s correspond to real tokens in enc_batch and target_batch; 0s correspond to padding.

        If hps.pointer_gen, additionally initializes the following:
            self.max_art_oovs:
                maximum number of in-article OOVs in the batch
            self.art_oovs:
                list of list of in-article OOVs (strings), for each example in the batch
            self.enc_batch_extend_vocab:
                Same as self.enc_batch, but in-article OOVs are represented by their temporary article OOV number.
        """
        # Determine the maximum length of the encoder input sequence in this batch
        max_enc_seq_len = max([ex.enc_len for ex in example_list])
        max_query_seq_len = max([ex.query_len for ex in example_list])

        # Pad the encoder input sequences up to the length of the longest sequence
        for ex in example_list:
            ex.pad_encoder_input(max_enc_seq_len, self.pad_id)
            ex.pad_query_input(max_query_seq_len, self.pad_id)

        # Initialize the numpy arrays
        # Note: our enc_batch can have different length (second dimension) for each batch because we use dynamic_rnn for the encoder.
        self.enc_batch = np.zeros((hps.batch_size, max_enc_seq_len), dtype=np.int32)
        self.enc_lens = np.zeros((hps.batch_size), dtype=np.int32)
        self.enc_padding_mask = np.zeros((hps.batch_size, max_enc_seq_len), dtype=np.float32)

        # query encoder part
        self.query_batch = np.zeros((hps.batch_size, max_query_seq_len), dtype=np.int32)
        self.query_lens = np.zeros((hps.batch_size), dtype=np.int32)
        self.query_padding_mask = np.zeros((hps.batch_size, max_query_seq_len), dtype=np.float32)

        # Fill in the numpy arrays
        for i, ex in enumerate(example_list):
            self.enc_batch[i, :] = ex.enc_input[:]
            self.enc_lens[i] = ex.enc_len
            for j in range(ex.enc_len):
                self.enc_padding_mask[i][j] = 1

            self.query_batch[i, :] = ex.query_input[:]
            self.query_lens[i] = ex.query_len
            for j in range(ex.query_len):
                self.query_padding_mask[i][j] = 1

        # For pointer-generator mode, need to store some extra info
        if hps.pointer_gen:
            # Determine the max number of in-article OOVs in this batch
            self.max_art_oovs = max([len(ex.oovs) for ex in example_list])
            # Store the in-article OOVs themselves
            self.art_oovs = [ex.oovs for ex in example_list]
            # Store the version of the enc_batch that uses the article OOV ids
            self.enc_batch_extend_vocab = np.zeros(
                (hps.batch_size, max_enc_seq_len), dtype=np.int32)
            self.query_batch_extend_vocab = np.zeros(
                (hps.batch_size, max_query_seq_len), dtype=np.int32)
            for i, ex in enumerate(example_list):
                self.enc_batch_extend_vocab[i, :] = ex.enc_input_extend_vocab[:]
                self.query_batch_extend_vocab[i, :] = ex.query_input_extend_vocab[:]

    def init_decoder_seq(self, example_list, hps):
        """Initializes the following:
        self.dec_batch:
            numpy array of shape (batch_size, max_dec_steps), containing integer ids as input for the decoder, padded to max_dec_steps length.
        self.target_batch:
            numpy array of shape (batch_size, max_dec_steps), containing integer ids for the target sequence, padded to max_dec_steps length.
        self.dec_padding_mask:
            numpy array of shape (batch_size, max_dec_steps), containing 1s and 0s. 1s correspond to real tokens in dec_batch and target_batch; 0s correspond to padding.
        """
        # Pad the inputs and targets
        for ex in example_list:
            ex.pad_decoder_inp_targ(hps.max_dec_steps, self.pad_id)

        # Initialize the numpy arrays.
        # Note: our decoder inputs and targets must be the same length for each batch (second dimension = max_dec_steps) because we do not use a dynamic_rnn for decoding. However I believe this is possible, or will soon be possible, with Tensorflow 1.0, in which case it may be best to upgrade to that.
        self.dec_batch = np.zeros(
            (hps.batch_size, hps.max_dec_steps), dtype=np.int32)
        self.target_batch = np.zeros(
            (hps.batch_size, hps.max_dec_steps), dtype=np.int32)
        self.dec_padding_mask = np.zeros(
            (hps.batch_size, hps.max_dec_steps), dtype=np.float32)

        # Fill in the numpy arrays
        for i, ex in enumerate(example_list):
            self.dec_batch[i, :] = ex.dec_input[:]
            self.target_batch[i, :] = ex.target[:]
            for j in range(ex.dec_len):
                self.dec_padding_mask[i][j] = 1

    def store_orig_strings(self, example_list):
        """Store the original article and abstract strings in the Batch object"""
        self.original_contexts = [ex.original_context for ex in example_list]
        self.original_summarizations = [ex.original_summarization for ex in example_list]
        self.original_querys = [ex.original_query for ex in example_list]


class Batcher(object):
    """A class to generate minibatches of data. 
    Buckets examples together based on length of the encoder sequence."""

    BATCH_QUEUE_MAX = 100  # max number of batches the batch_queue can hold

    def __init__(self, data_path, vocab, hps, single_pass):
        """Initialize the batcher. Start threads that process the data into batches.
        Args:
          data_path: tf.Example filepattern.
          vocab: Vocabulary object
          hps: hyperparameters
          single_pass: If True, run through the dataset exactly once 
                      (useful for when you want to run evaluation on the dev or test set). 
          Otherwise generate random batches indefinitely (useful for training).
        """
        self._data_path = data_path
        self._vocab = vocab
        self._hps = hps
        self._single_pass = single_pass

        # Initialize a queue of Batches waiting to be used, and a queue of Examples waiting to be batched
        self._batch_queue = queue.Queue(self.BATCH_QUEUE_MAX)
        self._example_queue = queue.Queue(
            self.BATCH_QUEUE_MAX * self._hps.batch_size)

        # Different settings depending on whether we're in single_pass mode or not
        if single_pass:
            self._num_example_q_threads = 1  # just one thread, so we read through the dataset just once
            self._num_batch_q_threads = 1  # just one thread to batch examples
            self._bucketing_cache_size = 1  # only load one batch's worth of examples before bucketing; this essentially means no bucketing
            self._finished_reading = False  # this will tell us when we're finished reading the dataset
        else:
            # 多线程随机性
            self._num_example_q_threads = 1  # 16 num threads to fill example queue
            self._num_batch_q_threads = 1  # 4 num threads to fill batch queue
            self._bucketing_cache_size = 100  # how many batches-worth of examples to load into cache before bucketing

        # Start the threads that load the queues
        self._example_q_threads = []
        for _ in range(self._num_example_q_threads):
            self._example_q_threads.append(
                Thread(target=self.fill_example_queue))
            self._example_q_threads[-1].daemon = True
            self._example_q_threads[-1].start()
        self._batch_q_threads = []
        for _ in range(self._num_batch_q_threads):
            self._batch_q_threads.append(Thread(target=self.fill_batch_queue))
            self._batch_q_threads[-1].daemon = True
            self._batch_q_threads[-1].start()

        # Start a thread that watches the other threads and restarts them if they're dead
        if not single_pass:  # We don't want a watcher in single_pass mode because the threads shouldn't run forever
            self._watch_thread = Thread(target=self.watch_threads)
            self._watch_thread.daemon = True
            self._watch_thread.start()

    def next_batch(self):
        """Return a Batch from the batch queue.

        If mode='decode' then each batch contains a single example repeated beam_size-many times; this is necessary for beam search.

        Returns:
          batch: a Batch object, or None if we're in single_pass mode and we've exhausted the dataset.
        """
        # If the batch queue is empty, print a warning
        if self._batch_queue.qsize() == 0:
            tf.logging.warning(
                'Bucket input queue is empty when calling next_batch. Bucket queue size: %i, Input queue size: %i',
                self._batch_queue.qsize(), self._example_queue.qsize())
            if self._single_pass and self._finished_reading:
                tf.logging.info(
                    "Finished reading dataset in single_pass mode.")
                return None

        batch = self._batch_queue.get()  # get the next Batch
        return batch

    def fill_example_queue(self):
        """Reads data from file and processes into Examples which are then placed into the example queue."""

        input_gen = self.text_generator(self._data_path, self._single_pass)

        while True:
            try:
                (context, summarization, query) = input_gen.next()
            except StopIteration:  # if there are no more examples:
                tf.logging.info(
                    "The example generator for this example queue filling thread has exhausted data."
                )
                if self._single_pass:
                    tf.logging.info(
                        "single_pass mode is on, so we've finished reading dataset. This thread is stopping."
                    )
                    self._finished_reading = True
                    break
                else:
                    raise Exception(
                        "single_pass mode is off but the example generator is out of data; error."
                    )

            # abstract_sentences = [
            #     sent.strip() for sent in data.abstract2sents(abstract)
            # ]  # Use the <s> and </s> tags in abstract to get a list of sentences.
            example = Example(context, summarization, query, self._vocab, self._hps)
            self._example_queue.put(example)

    def fill_batch_queue(self):
        """Takes Examples out of example queue, 
        sorts them by encoder sequence length, 
        processes into Batches and places them in the batch queue.

        In decode mode, makes batches that each contain a single example repeated.
        todo: why? 为什么decode时，重复
        """
        while True:
            if self._hps.mode != 'decode':
                # Get bucketing_cache_size-many batches of Examples into a list, then sort
                inputs = []
                for _ in range(
                        self._hps.batch_size * self._bucketing_cache_size):
                    inputs.append(self._example_queue.get())
                inputs = sorted(inputs, key=lambda inp: inp.enc_len)

                # Group the sorted Examples into batches, optionally shuffle the batches, and place in the batch queue.
                batches = []
                for i in range(0, len(inputs), self._hps.batch_size):
                    batches.append(inputs[i:i + self._hps.batch_size])
                if not self._single_pass:
                    shuffle(batches)
                for b in batches:  # each b is a list of Example objects
                    self._batch_queue.put(Batch(b, self._hps, self._vocab))

            else:  # beam search decode mode
                ex = self._example_queue.get()
                b = [ex for _ in range(self._hps.batch_size)]
                self._batch_queue.put(Batch(b, self._hps, self._vocab))

    def watch_threads(self):
        """Watch example queue and batch queue threads and restart if dead."""
        while True:
            time.sleep(60)
            for idx, t in enumerate(self._example_q_threads):
                if not t.is_alive():  # if the thread is dead
                    tf.logging.error(
                        'Found example queue thread dead. Restarting.')
                    new_t = Thread(target=self.fill_example_queue)
                    self._example_q_threads[idx] = new_t
                    new_t.daemon = True
                    new_t.start()
            for idx, t in enumerate(self._batch_q_threads):
                if not t.is_alive():  # if the thread is dead
                    tf.logging.error(
                        'Found batch queue thread dead. Restarting.')
                    new_t = Thread(target=self.fill_batch_queue)
                    self._batch_q_threads[idx] = new_t
                    new_t.daemon = True
                    new_t.start()

    def text_generator(self, data_path, single_pass):
        """Generates article and abstract text from tf.Example.

        Args:
            data_path:
            single_pass:
        """
        while True:
            filelist = glob.glob(data_path)  # get the list of datafiles
            assert filelist, ('Error: Empty filelist at %s' % data_path)
            if single_pass:
                filelist = sorted(filelist)
            else:
                random.shuffle(filelist)
            for f in filelist:
                with open(f) as train_f:
                    for line in train_f:
                        record = line.decode('utf-8').strip().split('\t\t')
                        if len(record) != 4:
                            continue
                        yield (record[0].strip() + '/' + record[1].strip(), record[3].strip(), record[2].strip())
            if single_pass:
                print("text_generator completed reading all datafiles. No more data.")
                break
