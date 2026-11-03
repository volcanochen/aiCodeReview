import os
import pickle
import requests


def foo(x=[]):
    x.append(1)
    return x


def bar():
    try:
        risky()
    except:
        pass


def baz():
    try:
        risky2()
    except Exception:
        pass


def run_cmd(cmd):
    os.system(cmd)


def evalme(s):
    return eval(s)


def check(v):
    if v == True:
        print("yes")


def load(data):
    return pickle.loads(data)


def fetch(url):
    return requests.get(url, verify=False)


print("debug output")
