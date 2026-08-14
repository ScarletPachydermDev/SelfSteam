Vendored third-party code, committed as plain source (not a git submodule).

- `Xlib/` — [python-xlib](https://github.com/python-xlib/python-xlib) 0.33,
  pure Python, LGPL 2.1 (see `Xlib-LICENSE`). Downloaded as a wheel and
  extracted as-is, unmodified. Used by `sync_gamescope_resolution.py`;
  vendored because the machines this runs on may have no `pip`
  available at runtime.
- `six.py` — [six](https://github.com/benjaminp/six) 1.17.0, MIT (see
  `six-LICENSE`). python-xlib's own Python 2/3 compat dependency.
- `darkreader.js` — [Dark Reader](https://github.com/darkreader/darkreader)
  4.9.128, MIT (see `DARKREADER-LICENSE.txt`). Used for the web UI's
  dark-mode toggle.
