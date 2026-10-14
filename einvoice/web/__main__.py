"""python -m einvoice.web  ->  starts the app and opens it in the browser."""
import threading
import webbrowser

from .app import create_app

HOST, PORT = "127.0.0.1", 5000


def main() -> None:
    app = create_app()
    url = f"http://{HOST}:{PORT}/"
    print(f"BIR e-Invoice running at {url}  (close this window to stop)")
    threading.Timer(1.0, webbrowser.open, args=[url]).start()
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
