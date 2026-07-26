from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _no_subject_phase(request):
    # A build reads its subject's phase off the durable engine; the tests that
    # don't stand one up must not reach for it. The *_durable tests run the real one.
    if "durable" in request.module.__name__:
        yield
        return

    async def _phase_noop(*args, **kwargs):
        pass

    with mock.patch("druks.contrib.ship.extension.get_subject_phase", _phase_noop):
        yield
