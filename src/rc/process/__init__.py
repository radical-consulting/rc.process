
from .process import Process

import os as _os

_vpath         = '%s/VERSION' % _os.path.dirname(__file__)
_data          = open(_vpath).read().split()
version        = _data[0].strip()
version_detail = _data[4].strip() if len(_data) > 1 else version
__version__    = version_detail

