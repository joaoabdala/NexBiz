# main.py
from app import app
import webbrowser

#webbrowser.open("http://127.0.0.1:5000")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)