'''
Provides process pools for vimap.

TBD:

For more complex tasks, which we might want to handle exceptions,

    def process_result(input):
        try:
            result = (yield)
            print("For input {0} got result {1}".format(input, result)
        except Exception as e:
            print("While processing input {0}, got exception {1}".format(input, e))

    processes.imap(entire_input_sequence).handle_result(process_result)

You can also use it in a more "async" manner, e.g. when your input sequences are
relatively small and/or calculated ahead of time, you can write,

    processes.map(seq1)
    processes.map(seq2)

(by default, input is only enqueued as results are consumed.)
'''
from __future__ import absolute_import
from __future__ import print_function
import itertools
import multiprocessing
import multiprocessing.queues
import sys


_IDLE_TIMEOUT = 0.02


def child_routine(fcn):
    def real_worker_routine(init_args, init_kwargs, input_queue, output_queue):
        '''
        Takes ordered items from input_queue, lets `fcn` iterate over
        those, and puts items yielded by `fcn` onto the output queue,
        with their IDs.
        '''
        i = [None] # just a mutable value
        def queue_generator():
            while True:
                try:
                    x = input_queue.get(timeout=_IDLE_TIMEOUT)
                    # print("Got {0} from input queue.".format(x))
                    if x is None:
                        return
                    i[0], z = x
                    yield z
                except multiprocessing.queues.Empty:
                    # print("Waiting")
                    pass
                except IOError as e:
                    print("Worker error getting item from input queue",
                        file=sys.stderr)
                    raise
        try:
            for output in fcn(queue_generator(), *init_args, **init_kwargs):
                assert i is not None, ("Produced output before getting first "
                    "input, or multiple outputs for one input.")
                output_queue.put( (i[0], 'output', output) )
                i[0] = None
        except Exception as e:
            output_queue.put( (i[0], 'exception', e) )

    return real_worker_routine


class Imap2Pool(object):
    '''Args: Sequence of imap2 workers.'''

    def __init__(self, worker_sequence):
        self._input_queue = multiprocessing.Queue()
        self._output_queue = multiprocessing.Queue()
        self.num_inflight = 0

        self.worker_sequence = worker_sequence
        self.processes = []

        self.input_uid_ctr = 0
        self.input_uid_to_input = {} # input to keep around until handled
        self.input_sequences = []
        self.finished_workers = False

    def fork(self):
        for worker in self.worker_sequence:
            process = multiprocessing.Process(
                target=child_routine(worker.fcn),
                args=(worker.args, worker.kwargs, self._input_queue, self._output_queue))
            process.start()
            self.processes.append(process)

    def __del__(self):
        '''Don't hang if all references to the pool are lost.'''
        self.finish_workers()

    def put_input(self, x):
        self.num_inflight += 1
        self._input_queue.put(x)

    def pop_output(self, *args, **kwargs):
        rv = self._output_queue.get(*args, **kwargs)
        self.num_inflight -= 1 # only decrement if no exceptions were thrown
        return rv

    def finish_workers(self):
        '''Sends stop tokens to subprocesses, then joins them.'''
        if not self.finished_workers:
            for _ in self.processes:
                self._input_queue.put(None)
            for process in self.processes:
                process.join()
            self.finished_workers = True

    # === Input-enqueueing functionality
    def imap(self, input_sequence, pretransform=False):
        '''Spools bits of an input sequence to workers' queues; good
        for doing things like iterating through large files, live
        inputs, etc. Otherwise, use map.

        Keyword arguments:
            pretransform -- if True, then assume input_sequence items
                are pairs (x, tf(x)), where tf is some kind of
                pre-serialization transform, applied to input elements
                before they are sent to worker processes.
        '''
        if pretransform:
            self.input_sequences.append(iter(input_sequence))
        else:
            self.input_sequences.append(((v, v) for v in input_sequence))
        self.spool_input(close_if_done=False)
        return self

    def map(self, *args, **kwargs):
        '''Like `imap`, but adds the entire input sequence.'''
        self.imap(*args, **kwargs).enqueue_all()
        return self

    @property
    def all_input(self):
        '''Input from all calls to imap; downside of this approach
        is that it keeps around dead iterators.
        '''
        return (x for seq in self.input_sequences for x in seq)

    def spool_input(self, close_if_done=True):
        '''Put input on the queue. Spools enough input for twice the
        number of processes.
        '''
        try:
            n_to_put = 2 * len(self.processes) - self._input_queue.qsize()
        except NotImplementedError:
            # Mac OS X workaround
            n_to_put = 2 * len(self.processes) # - self.num_inflight

        if n_to_put > 0:
            inputs = list(itertools.islice(self.all_input, n_to_put))
            for x in inputs:
                self.enqueue(x)
            if close_if_done and (not inputs):
                self.finish_workers()

    def enqueue(self, (x, xser)):
        '''
        Arguments:
            x -- the real input element
            xser -- the input element to be serialized and sent
                to the worker process
        '''
        uid = self.input_uid_ctr
        self.input_uid_ctr += 1
        self.input_uid_to_input[uid] = x

        try:
            self.put_input((uid, xser))
        except IOError as e:
            print("Error enqueueing item from main process", file=sys.stderr)
            raise

    def enqueue_all(self):
        '''Enqueue all input sequences assigned to this pool.'''
        for x in self.all_input:
            self.enqueue(x)
    # ------

    # === Results-consuming functions
    def zip_in_out(self, close_if_done=True):
        def has_output_or_inflight():
            '''returns True if there are processes alive, or items on the
            output queue.
            '''
            return (not self._output_queue.empty()) or (
                (sum(p.is_alive() for p in self.processes) > 0))

        self.spool_input(close_if_done=close_if_done)

        while self.input_uid_to_input and has_output_or_inflight():
            self.spool_input(close_if_done=close_if_done)
            try:
                uid, typ, output = self.pop_output(timeout=0.1)
                if typ == 'output':
                    yield self.input_uid_to_input.pop(uid), output
                elif typ == 'exception':
                    print("zip_in_out: Worker exception {0}".format(output),
                        file=sys.stderr)
            except multiprocessing.queues.Empty:
                pass
            except IOError as e:
                print("Error getting output queue item from main process",
                    file=sys.stderr)
                raise
        if close_if_done:
            self.finish_workers()
        # Return when input given is exhausted, or workers die from exceptions
    # ------

def fork(*args, **kwargs):
    pool = Imap2Pool(*args, **kwargs)
    pool.fork()
    return pool


def unlabeled_pool(worker_fcn, *args, **kwargs):
    '''Shortcut for when you don't care about per-worker initialization
    arguments.

    Example usage:

        parse_mykey = imap2.unlabeled_pool(
            lambda line: simplejson.loads(line)['mykey'])
        entries = parse_mykey.imap(fileinput.input())
    '''
    num_workers = kwargs.pop('num_workers', None)
    if num_workers is None:
        num_workers = multiprocesing.cpu_count()
    return pool(worker_fcn.init_args(*args, **kwargs)
        for _ in range(num_workers))