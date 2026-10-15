from einvoice import desktop
from einvoice.desktop import Presence
from tests.conftest import sign_in


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make():
    clock, stops = Clock(), []
    return Presence(on_empty=lambda: stops.append(clock.t), clock=clock), clock, stops


def test_closing_the_only_window_stops_after_grace():
    p, clock, stops = make()
    p.ping("a")
    clock.t = 5
    p.bye("a")
    clock.t = 8
    p.check()
    assert stops == []          # still within the grace period
    clock.t = 12
    p.check()
    assert stops == [12]


def test_page_change_is_not_a_close():
    p, clock, stops = make()
    p.ping("a")
    p.bye("a")                  # leaving one page...
    clock.t = 0.5
    p.ping("a")                 # ...the next page of the same window reports in
    clock.t = 30
    p.check()
    assert stops == []


def test_other_window_keeps_it_running():
    p, clock, stops = make()
    p.ping("a")
    p.ping("b")
    p.bye("a")
    clock.t = 10
    p.ping("b")
    p.check()
    assert stops == []
    p.bye("b")
    clock.t = 20
    p.check()
    assert stops == [20]


def test_minimised_window_with_slow_timers_survives():
    p, clock, stops = make()
    p.ping("a")
    clock.t = 60                # one ping a minute is enough
    p.ping("a")
    clock.t = 60 + desktop.STALE_SECONDS - 1
    p.check()
    assert stops == []
    clock.t = 60 + desktop.STALE_SECONDS + 1
    p.check()
    assert stops == [clock.t]   # a crashed/killed window eventually stops it


def test_no_window_ever_opened_stops_eventually():
    p, clock, stops = make()
    clock.t = desktop.FIRST_WINDOW_SECONDS - 1
    p.check()
    assert stops == []
    clock.t = desktop.FIRST_WINDOW_SECONDS + 1
    p.check()
    assert len(stops) == 1
    p.check()
    assert len(stops) == 1      # only once


def test_routes_and_script_only_in_desktop_mode(config):
    from einvoice.web.app import create_app
    plain = sign_in(create_app(config))
    assert "/_window/ping" not in plain.get("/").get_data(as_text=True)
    assert plain.post("/_window/ping").status_code in (404, 405)

    app = create_app(config)
    p, clock, stops = make()
    p.install(app)
    c = sign_in(app)
    assert "/_window/ping" in c.get("/").get_data(as_text=True)
    assert c.post("/_window/ping", data={"id": "w1"}).status_code == 204
    assert "w1" in p.last_seen
    c.post("/_window/bye", data={"id": "w1"})
    clock.t = 10
    p.check()
    assert stops == [10]
