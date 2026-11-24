# This python script holds the backend functions for client

import os
import socket

def send_file_to_server(server_socket, origin_filepath, destination_filepath) -> bool:
    # Bool conditional on success vs failure
    origin_filepath = os.path.abspath(origin_filepath) # Normalize the file_path to the absolute one

    # Check if the file actually exists
    if (os.path.isfile(origin_filepath) == False):
        print(f"Error: {origin_filepath} is not a file or does not exist")
        return False

    file_name = os.path.basename(origin_filepath)
    size_bytes = os.path.getsize(origin_fiepath)

    sendingHeader = json.dumps({"type": "Upload Request", "file_name": name, "file_path": destination_filepath, "file_size": size_bytes}) + "\n"
    server_socket.sendall(sendingHeader.encode("utf-8"))
    response = await_response_from_server(server_socket)
    if response is None:
        print("Error Occured with server response")
        return False
    else:
        if (response.get("type") == "Success"):
            print("File succesfully sent to server")
            return True
        elif (response.get("type") == "Fail"):
            print("File failed to be sent to server")
            if (response.get("fail") is not None):
                # If response reason is here, print it, otherwise don't print anything
                print(f"Failure Reason: {response.get("fail")}")
            return False

def recieve_file_from_server(server_socket,)

def await_response_from_server(server_socket):
            """ Awaits one header from the server, returns None if an error occured """
            header = server_socket.readline(4096) # Max limit on readline, should likely not be exceeded
            if not header:
                raise ConnectionError # We should always be able to read the header, otherwise connection failure

            # Attempt to process the header
            try:
                metadata = json.loads(header.decode("utf-8").strip()) # Strip should remove the newline character
            except JSONDecodeError:
                # Malformed header, don't attempt to process it further
                print("Unable to read response from server: Malformed Header")
                return None
            except Exception as e:
                # Other error, don't attempt to deal with it further
                print(f"Error parsing json: {e}")
                return None
            
            message_type = metadata["type"]
            return metadata
    
