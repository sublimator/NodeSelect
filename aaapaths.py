import sys

PATHS = [
 '/home/nick/.pythonbrew/pythons/Python-3.3.0/lib/python3.3/site-packages/distribute-0.6.32-py3.3.egg',
 '/home/nick/.pythonbrew/pythons/Python-3.3.0/lib',
 '/home/nick/.pythonbrew/pythons/Python-3.3.0/lib/python3.3',
 '/home/nick/.pythonbrew/pythons/Python-3.3.0/lib/python3.3/plat-linux',
 '/home/nick/.pythonbrew/pythons/Python-3.3.0/lib/python3.3/lib-dynload',
 '/home/nick/.pythonbrew/pythons/Python-3.3.0/lib/python3.3/site-packages',
 '/home/nick/.pythonbrew/pythons/Python-3.3.0/lib/python3.3/site-packages/setuptools-0.6c11-py3.3.egg-info']

for path in PATHS:
    if path not in sys.path:
        sys.path.insert(0, path)