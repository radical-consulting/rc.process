#!/usr/bin/env python3

import rc.process as rcp


# ------------------------------------------------------------------------------
#
def stdout_cb(proc, data):
    print('------------ stdout:\n %s\n--------------' % data)


def stderr_cb(proc, data):
    print('------------ stderr:\n %s\n--------------' % data)


def stdout_line_cb(proc, lines):
    for line in lines:
        print('STDOUT:', line)
        raise RuntimeError('oops')


def stderr_line_cb(proc, lines):
    for line in lines:
        print('STDERR:', line)


def state_cb(proc, state):
    print('state:', state)


# ------------------------------------------------------------------------------
#
if __name__ == '__main__':

    p = rcp.Process(cmd='sleep 2')
    p.start()
    p.wait()
    print('state:', p.state)

    p = rcp.Process(cmd='/bin/sh')
    p.register_cb(rcp.Process.CB_STATE,    state_cb)
    p.register_cb(rcp.Process.CB_OUT,      stdout_cb)
    p.register_cb(rcp.Process.CB_ERR,      stderr_cb)
    p.register_cb(rcp.Process.CB_OUT_LINE, stdout_line_cb)
    p.register_cb(rcp.Process.CB_ERR_LINE, stderr_line_cb)

    p.start()
    p.stdin('date\n')
    p.stdin('echo "foo\nbar\nbuz"\n')
    p.stdin('sleep 2\n echo "biz\nbaz\n"\n')
    p.stdin('sleep 2\n date\ndata\n sleep 1\n')

    p.stdin('echo "###"\n')
    p.find_line('^###$')


    print('-----------------')
    print(p.stdout)
    print('-----------------')
    print(p.stderr)
    print('=================')


    p.stdin('hostname; whoareyou; ')
    p.stdin('exit\n')
    p.wait()

    print('-----------------')
    print(p.stdout)
    print('-----------------')
    print(p.stderr)
    print('=================')


# ------------------------------------------------------------------------------


