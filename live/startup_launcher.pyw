import subprocess
import time
import os

#  Config
TARGET_SSID = "Link"

SCRIPTS_TO_RUN = [  
    r"C:\Users\Uyonoh\Documents\Coding\Python\automations\autofx\live\live_gemini.py"
]

PYTHON_EXE = r"C:\Users\Uyonoh\Documents\Coding\Python\automations\autofx\.venv\Scripts\pythonw.exe"

def check_and_connect_wifi():
    print(f"Checking for wifi: {TARGET_SSID}")

    subprocess.run('netsh interface set interface "Wi-Fi" admin=enabled state=connected', shell=True)

    cmd = 'netsh wlan show interfaces'
    result = subprocess.check_output(cmd, shell=True).decode('utf-8', errors='ignore')

    if TARGET_SSID in result:
        print("Connected to correct network")
        return True
    else:
        print("Not connected to target")
        # Might attemt to connect to different network
        return False
    
def launch_scripts():
    for script in SCRIPTS_TO_RUN:
        if os.path.exists(script):
            print(f"Launching {script}...")
            subprocess.Popen([PYTHON_EXE, script])
        else:
            print(f"Script not found: {script}")

if __name__ == "__main__":
    time.sleep(10)

    while not check_and_connect_wifi():
        time.sleep(10)
    
    launch_scripts()