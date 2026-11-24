# Portable test

Checks whether a Windows laptop will run Clonetron from a copied folder, with
nothing installed. Try it on any locked-down PC before relying on the app there.

## Test A: Python already on the PC

1. Copy this `portable-test` folder anywhere (Desktop, Documents or a USB stick).
2. Double-click **Start Clonetron test.bat**.
3. A black window opens, then a browser page that says **It works.**

If Windows says "Windows protected your PC", click **More info → Run anyway**.
If there is no Run anyway button, the PC blocks `.bat` files: open the folder,
click the address bar, type `cmd`, press Enter, and run `python hello.py`.

## Test B: no Python on the PC

1. Download the **Windows embeddable package (64-bit)** for Python 3.12 from
   <https://www.python.org/downloads/windows/>. It is a zip signed by the
   Python Software Foundation and needs no installing.
2. Unzip it into a folder called `python` inside this folder, so that
   `portable-test\python\python.exe` exists.
3. Double-click **Start Clonetron test.bat** again.

Send a screenshot of the result either way, including any error.
