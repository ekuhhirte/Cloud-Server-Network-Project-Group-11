# This file holds all the client work

import os # For checking if files exist (pathlib would also work)
import clientBackend # Functions from other python script
import socket # For TCP communication

HOST = "127.0.0.1" # Replace with GCP static IP address
PORT = 8080

# Main loop for input
while True:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((HOST, PORT))
        try:
            command = input("Choose one of the following commands using one letter: Download(D) Upload(U)")

            command = command.strip().upper() # Set to lowercase nad strip any newline or spaces
            if (command == "U"):
                print("Upload command was choosen")
                origin_filepath = input("Please input source file path")
                destination_filepath = input("Please input the destination file path")
                clientBackend.send_file_to_server(sock, origin_filepath, destination_filepath)
                # Collect other information and run the respective function call
            elif (command == "D"):
                print("Download command was choosen")
                pass
        except Exception as e:
            print(f"Error Occurred: {e}")
    # If an error occurs, while loop will attempt reconnection, might be better as a try except


    