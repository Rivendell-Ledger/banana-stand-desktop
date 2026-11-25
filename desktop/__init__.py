"""Desktop launcher package for The Banana Stand (Windows/Linux build).

Nothing in here is imported by the web app. The launcher sets the data
directories, starts uvicorn on loopback, opens the browser, and runs the
maintenance jobs that the Linux build normally gets from systemd timers.
"""
