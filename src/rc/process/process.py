#!/usr/bin/env python3

import re
import os
import sys
import time
import select

import subprocess as sp
import threading  as mt

from typing import Optional, Union


# ------------------------------------------------------------------------------
#
class Process(object):
    '''
    The `Process` class provides a simple interface to start and manage a
    background process. The class allows to register callbacks for different
    events, such as state changes, stdout and stderr output, and line buffered
    stdout and stderr output. The class also allows to write data to the
    process' stdin stream. The class is thread safe.

    Example:

        p = Process(cmd='/bin/sh')
        p.register_cb(Process.CB_STATE,    state_cb)
        p.register_cb(Process.CB_OUT,      stdout_cb)
        p.register_cb(Process.CB_ERR,      stderr_cb)
        p.register_cb(Process.CB_OUT_LINE, stdout_line_cb)
        p.register_cb(Process.CB_ERR_LINE, stderr_line_cb)

        p.start()
        p.stdin('date\n')
        p.stdin('exit 1\n')
        rc = print(p.wait())

        assert rc == 1
        print(p.stdout)

    '''

    # state enums
    NEW      = 'new'
    RUNNING  = 'running'
    DONE     = 'done'
    FAILED   = 'failed'
    CANCELED = 'canceled'
    FINAL    = [DONE, FAILED, CANCELED]

    # callback types
    CB_STATE    = 'state'
    CB_OUT      = 'out'
    CB_ERR      = 'err'
    CB_OUT_LINE = 'out_line'
    CB_ERR_LINE = 'err_line'

    # IO types
    _IO_OUT = 'out'
    _IO_ERR = 'err'


    # --------------------------------------------------------------------------
    #
    class _Buffer(object):
        '''
        we could use a simple string as buffer, but `handle_io()` wants to use
        buffer assignment by reference which Python does not nativly support for
        strings.
        '''

        def __init__(self, data=''): self._data = data
        def __str__ (self)         : return self._data
        def __add__ (self, other)  : return self._data + other
        def __radd__(self, other)  : return other + self._data
        def __iadd__(self, other)  : self._data += other; return self
        def __bool__(self)         : return bool(self._data)
        def clear   (self)         : self._data = ''


    # --------------------------------------------------------------------------
    #
    def __init__(self, cmd     : str,
                       env     : Optional[dict            ] = None,
                       shell   : Optional[bool            ] = True,
                       stdin   : Optional[Union[bool, str]] = True,
                       stdout  : Optional[Union[bool, str]] = True,
                       stderr  : Optional[Union[bool, str]] = True,
                       ) -> None:

        '''
        Create a new process instance. The process is not started yet, but can
        be started via the `start()` method.

        Args:

          cmd     (str)      : The command to be executed.
          env     (dict)     : environment to use for the process
          shell   (bool)     : `True` : run the command in a shell
          stdin   (bool, str): `True` : allow input to the process via stdin
                               `False`: do not allow input to the process
                               `str`  : data to feed to stdin (implies `True`)
          stdout  (bool, str): `False`: discard the process' stdout stream
                               `True` : capture the process' stdout stream
                               `str`  : filename to copy the stdout stream to
          stderr  (bool, str): `False`: discard the process' stderr stream
                               `True` : capture the process' stderr stream
                               `str`  : filename to copy the stderr stream to

        Note that all I/O are string based, not byte based.
        '''

        self._cmd    = cmd
        self._shell  = shell
        self._env    = env
        self._stdin  = stdin
        self._stdout = stdout
        self._stderr = stderr

        self._cbs = {self.CB_STATE   : list(),
                     self.CB_OUT     : list(),
                     self.CB_ERR     : list(),
                     self.CB_OUT_LINE: list(),
                     self.CB_ERR_LINE: list()}

        self._proc      = None
        self._retcode   = None
        self._state     = None
        self._lock      = mt.Lock()
        self._cancel    = mt.Event()

        self._buf_in    = self._Buffer()  # hold stdin until we can write it
        self._buf_out   = self._Buffer()  # collect stdout
        self._buf_err   = self._Buffer()  # collect stderr

        self._lbuf_out  = self._Buffer()  # hold stdout until next newline
        self._lbuf_err  = self._Buffer()  # hold stderr until next newline

        self._fout      = None            # file handle for stdout
        self._ferr      = None            # file handle for stderr

        self._idx_in    = 0
        self._idx_out   = 0
        self._idx_err   = 0

        self._bufsize   = 1024
        self._polldelay = 0.5

        self._advance(self.NEW)


    # --------------------------------------------------------------------------
    #
    def __del__(self) -> None:

        try:
            self.cancel()
            self._cleanup()
        except:
            # ignore all errors on cleanup
            pass


    # --------------------------------------------------------------------------
    #
    def __enter__(self) -> 'Process':
        return self


    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Called on exit from `with ... as p:`
        # Ensure the process is stopped/cleaned up no matter what
        try:
            if self.state not in self.FINAL:
                self.cancel()
        finally:
            self._cleanup()  # close files, set _proc=None, etc.


    # --------------------------------------------------------------------------
    #
    @property
    def bufsize(self) -> int:
        '''
        Return the buffer size used for reading from the process' stdout and
        stderr streams.
        '''
        return self._bufsize


    @bufsize.setter
    def bufsize(self, bufsize: int) -> None:
        '''
        Set the buffer size used for reading from the process' stdout and
        stderr streams.
        '''
        self._bufsize = bufsize


    @property
    def polldelay(self) -> float:
        '''
        Return the poll delay used for reading from the process' stdout and
        stderr streams, in seconds.
        '''
        return self._polldelay

    @polldelay.setter
    def polldelay(self, polldelay: float) -> None:
        '''
        Set the poll delay used for reading from the process' stdout and
        stderr streams, in seconds.
        '''
        self._polldelay = polldelay


    @property
    def stdout(self) -> str:
        '''
        Return the captured stdout of the process. The stdout is only valid
        after the process has finished.  If the stdout capture was disabled,
        the method will return `None`.
        '''
        if self._stdout is False:
            return None
        with self._lock:
            ret = str(self._buf_out)
            self._buf_out.clear()
        return ret


    @property
    def stderr(self) -> str:
        '''
        Return the captured stderr of the process. The stderr is only valid
        after the process has finished.  If the stderr capture was disabled,
        the method will return `None`.
        '''
        if self._stderr is False:
            return None

        with self._lock:
            ret = str(self._buf_err)
            self._buf_err.clear()
        return ret


    @property
    def state(self) -> str:
        '''
        Return the current state of the process. The state is one of the
        following values:

            - `Process.NEW`     : The process has been created but not started.
            - `Process.RUNNING` : The process is running.
            - `Process.DONE`    : The process has finished successfully.
            - `Process.FAILED`  : The process has finished with an error.
            - `Process.CANCELED`: The process has been canceled.
        '''
        return self._state

    @property
    def retcode(self) -> Optional[int]:
        '''
        Return the return code of the process. The return code is only valid
        after the process has finished.
        '''
        return self._retcode


    # --------------------------------------------------------------------------
    #
    def stdin(self, data: str) -> None:
        '''
        Write data to the process' stdin stream.

        Args:

          data (str): The data to write to the process' stdin stream.
        '''
        with self._lock:

            if self._state in self.FINAL:
                raise RuntimeError('stdin unavailable, process is not running.')

            self._buf_in += data


    # --------------------------------------------------------------------------
    #
    def find_line(self, pattern: str, timeout: Optional[float] = None) -> None:
        '''
        Find a line in the process' stdout stream that matches the given pattern.
        The method will block until the pattern has been found or until the
        timeout has been reached.
        '''

        pattern = re.compile(pattern)

        start = time.time()
        while True:

            with self._lock:
                lines = str(self._buf_out).split('\n')

            for line in lines:
                if pattern.match(line):
                    return line

            if timeout is not None:
                if start - time.time() > timeout:
                    return None

            if self._state in self.FINAL:
                raise RuntimeError('find_line failed, process died.')

            time.sleep(self._polldelay)


    # --------------------------------------------------------------------------
    #
    def _cleanup(self) -> None:

        with self._lock:

            if self._fout: self._fout.close()
            if self._ferr: self._ferr.close()

            self._fout = None
            self._ferr = None

            self._proc = None


    # --------------------------------------------------------------------------
    #
    def _advance(self, state) -> None:

        with self._lock:
            if self._state in self.FINAL:
                return

            self._state = state
            cbs = list(self._cbs[self.CB_STATE])

        for cb in cbs:
            try:
                cb(self, state)
            except Exception as e:
                self._handle_error(e)


    # --------------------------------------------------------------------------
    #
    def cancel(self) -> None:

        '''
        Cancel the process.  This call returns immediately, and the process will
        be canceled asynchronously.  Once canceled, the state will be set to
        `Process.CANCELED`.
        '''

        with self._lock:
            if self._proc is None:
                return

            self._cancel.set()
            self._proc.terminate()


    # ---------------------------------------------------------------------------
    #
    def kill(self) -> None:
        return self.cancel()


    # --------------------------------------------------------------------------
    #
    def signal(self, sig: int) -> None:
        '''
        Send a signal to the process.  The signal will be sent asynchronously,
        and the state will not be changed.
        '''

        with self._lock:
            if self._proc is None:
                return

            os.kill(self._proc.pid, sig)


    # --------------------------------------------------------------------------
    #
    def register_cb(self, cb_type: str, cb: callable) -> None:
        '''
        Register a callback for a specific event type.

        Args:

          cb_type (str): The type of event to register the callback for. The
                         following event types are supported:

                         - `CB_STATE`    : state change
                         - `CB_OUT`      : stdout (char buffered)
                         - `CB_ERR`      : stderr (char buffered)
                         - `CB_OUT_LINE` : stdout (line buffered)
                         - `CB_ERR_LINE` : stderr (line buffered)

          cb (callable): The callback to register. The callback signatures differ
                         depending on the event type:

                         - `CB_STATE`   : cb(proc: Process, state: str)
                         - `CB_OUT`     : cb(proc: Process, data:  str)
                         - `CB_ERR`     : cb(proc: Process, data:  str)
                         - `CB_OUT_LINE`: cb(proc: Process, lines: List[str])
                         - `CB_err_LINE`: cb(proc: Process, lines: List[str])
        '''

        if cb_type not in self._cbs:
            raise ValueError('invalid cb_type: %s' % cb_type)

        with self._lock:
            self._cbs[cb_type].append(cb)

    # --------------------------------------------------------------------------
    #
    def wait(self, timeout: Optional[float] = None) -> None:
        '''
        Wait for the process to finish. The method will block until the
        process has finished, or until the timeout has been reached.
        '''

        if self._proc is None:
            return

        start = time.time()
        while True:

            with self._lock:
                if self._state in self.FINAL:
                    return self._retcode

            if timeout is not None:
                if time.time() - start > timeout:
                    break

            time.sleep(self._polldelay)


    # --------------------------------------------------------------------------
    #
    def start(self) -> None:
        '''
        Start the process. The process will run in the background, and the
        state will be set to `Process.RUNNING`. The process' stdout and stderr
        streams will be read and processed asynchronously.
        '''

        started = mt.Event()

        self._thread = mt.Thread(target=self._watch, args=[started])
        self._thread.daemon = True
        self._thread.start()

        started.wait()


    # --------------------------------------------------------------------------
    #
    def _safe_watch(self, started: mt.Event) -> None:
        '''
        Wrapper around `_watch()` to catch exceptions and set the state to
        `Process.FAILED` in case of an error.
        '''

        try:
            self._watch(started)

        except Exception as e:
            with self._lock:
                if self._proc:
                    self._proc.terminate()
            self._advance(self.FAILED)
            raise RuntimeError('process watcher failed') from e


    # --------------------------------------------------------------------------
    #
    def _watch(self, started: mt.Event) -> None:
        '''
        Watch the process' stdout and stderr streams and handle the I/O
        accordingly. The method will block until the process has finished or is
        being cancaled.  Once completed, the method will set the process' return
        code and state accordingly.

        '''

        def _open(fname: str, mode: str):
            return open(fname, mode, buffering=1, encoding='utf-8')

        if isinstance(self._stdout, str): self._fout = _open(self._stdout, 'w')
        if isinstance(self._stderr, str): self._ferr = _open(self._stderr, 'w')
        if isinstance(self._stdin,  str): self._buf_in = self._stdin

        spec_in  = None if self._stdin  is False else sp.PIPE
        spec_out = None if self._stdout is False else sp.PIPE
        spec_err = None if self._stderr is False else sp.PIPE

        self._proc = sp.Popen(self._cmd,
                              env=self._env, shell=self._shell, text=True,
                              stdin=spec_in, stdout=spec_out, stderr=spec_err)

        started.set()

        self._advance(self.RUNNING)

        poll_in  = None
        poll_out = None

        if spec_in:
            poll_in = select.poll()
            poll_in.register(self._proc.stdin,  select.POLLOUT)

        if spec_out or spec_err:
            poll_out = select.poll()
            if spec_out:
                poll_out.register(self._proc.stdout, select.POLLIN)
            if spec_err:
                poll_out.register(self._proc.stderr, select.POLLIN)

        while True:

            if poll_in and self._buf_in:

                if poll_in.poll():

                    with self._lock:
                        buf_in       = self._buf_in
                        self._buf_in = ''

                    self._proc.stdin.write(str(buf_in))
                    try: self._proc.stdin.flush()
                    except BrokenPipeError: pass  # skip to `proc.poll()`

            # poll for all available data on stdout and stderr
            poll_ok = True
            while poll_out and poll_ok:

                streams_out = poll_out.poll(self._polldelay * 1000)

                if not streams_out:
                    # nothing more to read
                    break

                for fd, ev in streams_out:

                    if ev in [select.POLLERR, select.POLLHUP, select.POLLNVAL]:
                        # skip to `proc.poll()`
                        poll_ok = False
                        continue

                    if fd == self._proc.stdout.fileno():
                        data = os.read(fd, self._bufsize)
                        self._handle_io(self._IO_OUT, data)

                    if fd == self._proc.stderr.fileno():
                        data = os.read(fd, self._bufsize)
                        self._handle_io(self._IO_ERR, data)

            ret = self._proc.poll()
            if ret is not None:
                if self._cancel.is_set():
                    self._advance(self.CANCELED)
                elif ret == 0:
                    self._retcode = 0
                    self._advance(self.DONE)
                else:
                    self._retcode = ret
                    self._advance(self.FAILED)
                break

            # avoid busy poll
            if not self._buf_in and not poll_out:
                time.sleep(self._polldelay)

        self._cleanup()


    # --------------------------------------------------------------------------
    #
    def _handle_io(self, io_type: str, data: bytes) -> None:
        '''
        Handle incoming data from the process' stdout or stderr streams and
        call the registered callbacks.  The method will also write the data to
        the corresponding file handle, if available.

        Args:

            io_type (str): The type of the stream to handle. The following types
                           are supported:

                           - `Process._IO_OUT`: stdout
                           - `Process._IO_ERR`: stderr

            data (bytes): The data read from the stream.
        '''

        assert io_type in [self._IO_OUT, self._IO_ERR], io_type

        if io_type == self._IO_OUT:

            buf  = self._buf_out
            lbuf = self._lbuf_out
            cbb  = self._cbs[self.CB_OUT]
            cbl  = self._cbs[self.CB_OUT_LINE]
            fio  = self._fout

        elif io_type == self._IO_ERR:

            buf  = self._buf_err
            lbuf = self._lbuf_err
            cbb  = self._cbs[self.CB_ERR]
            cbl  = self._cbs[self.CB_ERR_LINE]
            fio  = self._ferr

        else:
            # keep linter happy
            return

        if fio:
            fio.write(data)
            fio.flush()

        sdata = data.decode('utf-8', errors='replace')

        buf += sdata
        for cb in cbb:
            try:
                cb(self, sdata)
            except Exception as e:
                self._handle_error(e)

        if '\n' not in sdata:
            lbuf += sdata

        else:
            lines    = sdata.split('\n')
            lines[0] = lbuf + lines[0]

            # If the last line is incomplete (`lines[-1]` is not empty), keep it
            # in the buffer.  If it is complete, then `lines[-1]` will be empty
            # and we also can assign it to the buffer.
            lbuf.clear()
            lbuf += lines.pop()

            for cb in cbl:
                try:
                    cb(self, lines)
                except Exception as e:
                    self._handle_error(e)


    # --------------------------------------------------------------------------
    #
    def _handle_error(self, e: Exception) -> None:
        '''
        Handle an exception raised by a callback. The method will print the
        exception to stderr.
        '''
        import traceback
        traceback.print_exc()
        print('callback error: %s' % e, file=sys.stderr)


# ------------------------------------------------------------------------------

