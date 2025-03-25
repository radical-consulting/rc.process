#!/usr/bin/env python3

import time
import unittest
import unittest.mock

from rc.process import Process


# ------------------------------------------------------------------------------
#
class TestProcess(unittest.TestCase):

    def setUp(self):
        """
        Called before each test. We'll create a fresh Process instance
        for each test to ensure isolation.
        """
        self.cmd = 'echo test'
        self.proc = Process(cmd=self.cmd)


    # --------------------------------------------------------------------------
    #
    def test_init(self):
        """Test that the process is initialized correctly."""
        self.assertEqual(self.proc.state, Process.NEW)
        self.assertIsNone(self.proc._proc)
        self.assertIsNone(self.proc.retcode)

        # Verify the callback dictionaries are initialized and empty
        self.assertIn(Process.CB_STATE,    self.proc._cbs)
        self.assertIn(Process.CB_OUT,      self.proc._cbs)
        self.assertIn(Process.CB_ERR,      self.proc._cbs)
        self.assertIn(Process.CB_OUT_LINE, self.proc._cbs)
        self.assertIn(Process.CB_ERR_LINE, self.proc._cbs)

        for cb_list in self.proc._cbs.values():
            self.assertEqual(len(cb_list), 0)


    # --------------------------------------------------------------------------
    #
    def test_register_cb(self):
        """Test that callbacks can be registered correctly."""

        def dummy_cb(*args):
            pass

        # Register a callback
        self.proc.register_cb(Process.CB_OUT, dummy_cb)
        self.assertIn(dummy_cb, self.proc._cbs[Process.CB_OUT])

        # Attempt an invalid type
        with self.assertRaises(ValueError):
            self.proc.register_cb('invalid_cb_type', dummy_cb)


    # --------------------------------------------------------------------------
    #
    def test_stdin(self):
        """
        Test that writing to stdin (via .stdin()) appends data to _buf_in
        correctly.
        """
        self.assertEqual(str(self.proc._buf_in), "")  # initially empty

        self.proc.stdin("hello ")
        self.proc.stdin("world")
        self.assertEqual(str(self.proc._buf_in), "hello world")


    # --------------------------------------------------------------------------
    #
    @unittest.mock.patch('subprocess.Popen')
    def test_start(self, mock_popen):
        """
        Test start() to ensure a background thread is spawned and
        subprocess.Popen is invoked with correct parameters.
        """
        # Mock the Popen object itself:
        mock_proc = unittest.mock.MagicMock()
        mock_proc.poll.return_value = None  # Pretend it's running
        mock_popen.return_value = mock_proc

        # Start the process
        self.proc.start()

        # Verify Popen was called
        mock_popen.assert_called_once()
        self.assertEqual(self.proc.state, Process.RUNNING)

        # Ensure the background thread has set self._proc
        self.assertIsNotNone(self.proc._proc)


    # --------------------------------------------------------------------------
    #
    def test_wait(self):
        """
        Test wait() to see if it blocks until the process completes.
        We'll mock poll() to simulate exit.
        """
        proc = Process(cmd='sleep 1', shell=True)
        proc.start()
        start_time = time.time()
        retcode = proc.wait(timeout=2.0)
        end_time = time.time()

        self.assertEqual(retcode, 0)
        self.assertLess(end_time - start_time, 2.0, "wait() took too long")

        # Now the state should be DONE
        self.assertEqual(proc.state, Process.DONE)
        self.assertEqual(proc.retcode, 0)


    # --------------------------------------------------------------------------
    #
    def test_cancel(self):
        """
        Test that cancel() terminates the process, sets state to CANCELED,
        and performs cleanup.
        """

        proc = Process(cmd='sleep 2', shell=True)
        proc.start()

        time.sleep(0.1)  # Give it a moment to start

        # Cancel the process
        proc.cancel()
        proc.wait()
        self.assertTrue(proc._cancel.is_set())
        self.assertEqual(proc.state, Process.CANCELED)


    # --------------------------------------------------------------------------
    #
    def test_handle_io_chunk_cb(self):
        """
        Test that _handle_io calls chunk-based callbacks (CB_OUT, CB_ERR)
        with the correct data.
        """
        out_collected = []
        err_collected = []

        def out_cb(proc, data):
            out_collected.append(data)

        def err_cb(proc, data):
            err_collected.append(data)

        self.proc.register_cb(Process.CB_OUT, out_cb)
        self.proc.register_cb(Process.CB_ERR, err_cb)

        # Manually invoke _handle_io
        self.proc._handle_io(Process._IO_OUT, "hello out")
        self.proc._handle_io(Process._IO_ERR, "hello err")

        self.assertEqual(out_collected, ["hello out"])
        self.assertEqual(err_collected, ["hello err"])


    # --------------------------------------------------------------------------
    #
    def test_handle_io_line_cb(self):
        """
        Test that _handle_io does line-based buffering and calls
        line-based callbacks with full lines.
        """
        out_lines_collected = []
        err_lines_collected = []

        def out_line_cb(proc, lines):
            out_lines_collected.extend(lines)

        def err_line_cb(proc, lines):
            err_lines_collected.extend(lines)

        self.proc.register_cb(Process.CB_OUT_LINE, out_line_cb)
        self.proc.register_cb(Process.CB_ERR_LINE, err_line_cb)

        # Manually feed partial lines and complete lines
        self.proc._handle_io(Process._IO_OUT, "line1 part")
        self.proc._handle_io(Process._IO_OUT, "ial\nline2\nline3 no newline")

        self.proc._handle_io(Process._IO_ERR, "err1\nerr2\n")
        self.proc._handle_io(Process._IO_ERR, "err3 partial")

        # The line-based callbacks only fire on newlines.
        self.assertEqual(out_lines_collected, ["line1 partial", "line2"])
        self.assertEqual(err_lines_collected, ["err1", "err2"])

        # The partial lines remain buffered internally.
        self.assertEqual(str(self.proc._lbuf_out), "line3 no newline")
        self.assertEqual(str(self.proc._lbuf_err), "err3 partial")


    # --------------------------------------------------------------------------
    #
    @unittest.mock.patch('subprocess.Popen')
    def test_stdout_stderr_properties(self, mock_popen):
        """
        Test that stdout and stderr properties are None if disabled,
        or return captured data if enabled.
        """
        # Create a process with stdout=False, stderr=True
        proc_no_stdout = Process(cmd='ls', stdout=False, stderr=True)
        self.assertIsNone(proc_no_stdout.stdout)
        self.assertIsNotNone(proc_no_stdout.stderr)
        # It's empty string by default until run finishes

        # Similarly, we can create one with stdout=True, stderr=False, etc.
        proc_no_stderr = Process(cmd='ls', stdout=True, stderr=False)
        self.assertIsNotNone(proc_no_stderr.stdout)
        self.assertIsNone(proc_no_stderr.stderr)


    # --------------------------------------------------------------------------
    #
    def test_bufsize_property(self):
        """Simple test of getting/setting bufsize."""
        self.proc.bufsize = 2048
        self.assertEqual(self.proc.bufsize, 2048)


    # --------------------------------------------------------------------------
    #
    def test_polldelay_property(self):
        """Simple test of getting/setting polldelay."""
        self.proc.polldelay = 0.05
        self.assertEqual(self.proc.polldelay, 0.05)


# ------------------------------------------------------------------------------
#
if __name__ == '__main__':

    tc = TestProcess()
    tc.setUp()
  # tc.test_init()
  # tc.test_register_cb()
  # tc.test_stdin()
  # tc.test_start()
    tc.test_wait()
  # tc.test_cancel()
  # tc.test_handle_io_chunk_cb()
  # tc.test_handle_io_line_cb()
  # tc.test_stdout_stderr_properties()
  # tc.test_bufsize_property()
  # tc.test_polldelay_property()
    tc.tearDown()


# ------------------------------------------------------------------------------

