import pytest
from urllib.error import URLError

from scripts.verify_publication import verify


class Clock:
    now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def files(tmp_path):
    # Delivery verification must also accept a correctly published blocked page.
    (tmp_path / 'health.json').write_bytes(b'{"status":"blocked"}')
    (tmp_path / 'index.html').write_bytes(b'blocked page')
    return tmp_path


def test_verifies_both_exact_files(files):
    seen = []
    def fetch(url, timeout):
        seen.append(url.rsplit('/', 1)[1])
        assert 0 < timeout <= 20
        return (files / seen[-1]).read_bytes()
    verify(files, fetch=fetch)
    assert seen == ['health.json', 'index.html']


@pytest.mark.parametrize('failure', ['old_health', 'old_page', 'http'])
def test_retries_propagation_and_transient_http_failure(files, failure):
    clock = Clock()
    def fetch(url, timeout):
        name = url.rsplit('/', 1)[1]
        if clock.now == 0:
            if failure == 'http':
                raise URLError('temporary outage')
            if name == ('health.json' if failure == 'old_health' else 'index.html'):
                return b'previous build'
        return (files / name).read_bytes()
    verify(files, timeout=30, interval=10, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert clock.now == 10


def test_never_treats_successful_health_only_as_live(files):
    clock = Clock()
    def fetch(url, timeout):
        return (files / 'health.json').read_bytes() if url.endswith('health.json') else b'stale html'
    with pytest.raises(RuntimeError, match='public index.html does not match'):
        verify(files, timeout=22, interval=10, fetch=fetch, clock=clock, sleep=clock.sleep)
    assert clock.now == 22


def test_each_request_is_bounded_by_remaining_budget(files):
    clock = Clock()
    def fetch(url, timeout):
        assert timeout <= 3 - clock.now
        clock.now += 2
        return (files / url.rsplit('/', 1)[1]).read_bytes()
    with pytest.raises(RuntimeError, match='not verified within'):
        verify(files, timeout=3, fetch=fetch, clock=clock, sleep=clock.sleep)


@pytest.mark.parametrize('timeout,interval', [(0, 1), (1, 0), (-1, 1)])
def test_rejects_unbounded_or_invalid_timing(files, timeout, interval):
    with pytest.raises(ValueError):
        verify(files, timeout=timeout, interval=interval)


def test_missing_local_file_fails_without_network(tmp_path):
    with pytest.raises(FileNotFoundError):
        verify(tmp_path, fetch=lambda *a, **k: pytest.fail('unexpected network'))
