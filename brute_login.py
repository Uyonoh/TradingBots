import itertools
import MetaTrader5 as mt5
import time

# --- CONFIGURATION ---
min_password_length = 6
ACCOUNT_NUMBER = 1301472224  # Replace with your demo account number
SERVER_NAME = "XMGlobal-MT5 6"  # Replace with your specific broker server name

# The base words or fragments you typically use
# BASE_WORDS = ["HGX7BEu3frC9i0rfPlyt:a85a55a7bb5e2052fc036daf2fadc94158699fddcc8db5b4fe5095c655cc161e"]
BASE_WORDS = ["if", "(", ")", "Demo", "1", "open"]
BASE_WORDS =["#1","demo", "69e5ed250aa8167b6533ad2c", "a1d8c6c9-54a2-481e-915a-3f2bd7e055af", "414631850", "weeklyDemoLimit"]


def generate_passwords(words):
    """Generates password combinations based on your common patterns.

    Modify this logic to match how you typically build passwords.
    """
    combinations = set()

    # Pattern 1: Capitalization variations of single words
    for word in words:
        combinations.add(word.lower())
        combinations.add(word.upper())
        combinations.add(word.capitalize())

    # Pattern 2: Joining 2 or 3 fragments together directly
    # Adjust 'r' to change how many fragments you stitch together
    for r in range(1, 4):
        for perm in itertools.permutations(words, r):
            combinations.add("".join(perm))
            # Optional: Add variations with common separators if you use them
            # combinations.add("_".join(perm))

    return list(combinations)


def test_logins(account, server, password_list):
    print(f"[*] Initializing MT5 connection...")
    if not mt5.initialize():
        print(f"[-] MT5 initialization failed. Error code: {mt5.last_error()}")
        return

    print(f"[*] Generated {len(password_list)} potential password variations.")
    print("[*] Starting recovery attempts...")

    for i, password in enumerate(password_list, 1):
        print(f"[{i}/{len(password_list)}] Testing: {password}")
        if len(password) < min_password_length:
            print(f"{len(password)} < {min_password_length}")
            continue

        # Attempt authorization
        authorized = mt5.login(account, password=password, server=server)

        if authorized:
            print("\n" + "=" * 40)
            print("[+] SUCCESS! Account recovered.")
            print(f"[+] Password found: {password}")
            print("=" * 40 + "\n")

            # Print account details to confirm active connection
            account_info = mt5.account_info()
            if account_info is not None:
                print(f"Account Name: {account_info.name}")
                print(f"Balance: {account_info.balance}")
            break
        else:
            # If the error code indicates a connection or terminal issue rather than
            # wrong credentials, it's good to catch it early.
            err_code, err_msg = mt5.last_error()

            # -10005 typically indicates an IPC timeout (terminal is busy or closed)
            if err_code == -10005:
                print("Terminal issue detected: IPC Timeout. Restarting terminal/script...")
                break
            else:
                print(f"General Init Error [{err_code}]: {err_msg}")
                time.sleep(0.2)

    else:
        print("\n[-] All password variations exhausted. Match not found.")

    # Always clean up the connection when done
    mt5.shutdown()


if __name__ == "__main__":
    # 1. Generate the list of variations
    passwords_to_test = generate_passwords(BASE_WORDS)

    # 2. Run the testing sequence
    test_logins(ACCOUNT_NUMBER, SERVER_NAME, passwords_to_test)
