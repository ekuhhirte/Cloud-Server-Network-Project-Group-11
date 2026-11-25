# This python script holds the backend functions for client

import os
import socket
import json

def send_file_to_server(server_socket, server_socket_makefile, origin_filepath, destination_filepath) -> bool:
    # Bool conditional on success vs failure
    origin_filepath = os.path.abspath(origin_filepath) # Normalize the file_path to the absolute one

    # Check if the file actually exists
    if (os.path.isfile(origin_filepath) == False):
        print(f"Error: {origin_filepath} is not a file or does not exist")
        return False

    # Get information about the file (name and size)
    file_name = os.path.basename(origin_filepath)
    size_bytes = os.path.getsize(origin_filepath)

    # Make and send header to the server
    sendingHeader = json.dumps({"type": "Upload Request", "file_name": file_name, "file_path": destination_filepath, "file_size": size_bytes}) + "\n"
    server_socket.sendall(sendingHeader.encode("utf-8"))

    # Send file to server
    with open(origin_filepath, 'rb') as file: #MUST BE OPENED IN BINARY
        server_socket.sendfile(file)

    # Await response from the server
    response = await_response_from_server(server_socket_makefile)
    print(f"Response Packet from Server: {response}") # Debug Code

    # Read response from server if it exists
    if response is None:
        print("Error Occured with server response")
        return False
    else:
        if (response.get("type") == "Upload Response"):
            print("File succesfully sent to server")
            return True
        elif (response.get("type") == "error"):
            print("File failed to be sent to server")
            if (response.get("error") is not None):
                # If response reason is here, print it, otherwise don't print anything
                print(f"Failure Reason: {response.get('error')}")
            return False

def recieve_file_from_server(server_socket, server_socket_makefile,  server_filepath) -> bool:
    """ Requests and Recieves a file from the server """

    # Generate and send request header
    sendingHeader = json.dumps({"type": "Download Request", "file_path": server_filepath}) + "\n"
    server_socket.sendall(sendingHeader.encode("utf-8"))

    # Wait for return header from server
    response = await_response_from_server(server_socket_makefile)
    print(f"Response Packet from Server: {response}") # Debug Code
    
    # Read response from the server if it exists
    if response is None:
        print("Error Occurred with server response")
        # Might need to clear socket buffer afterwards?
        clear_buffer(server_socket, server_socket_makefile)
        return False
    elif (response.get("type") == "Download Response"):
        # We got the right packet, safely grab the information out of the header
        filename = response.get("filename")
        file_size = response.get("file_size")
        if (filename is None or file_size is None):
            print(f"Error: Malformed Packet from Server")
            # Maybe need to clear socket buffer?
            clear_buffer(server_socket, server_socket_makefile)
            return False

        # Write data to a file
        # Todo: Add support for command line interface specifiying a file path for it to download to,
        # Not a priority for the test though.

        bytes_recieved = 0
        chunk_Size = 4096
        with open(filename, "wb") as file:
            while bytes_recieved < file_size:
                chunk = server_socket.recv(min(chunk_Size, (file_size - bytes_recieved)))
                if not chunk:
                    print(f"Error: Connection closed early: Expected {file_size} bytes. Recieved {bytes_recieved} bytes. Difference: {file_size - bytes_recieved}")
                    return False
                file.write(chunk)
                bytes_recieved += len(chunk) 
        return True



def await_response_from_server(server_socket_makefile):
            """ Awaits one header from the server, returns None if an error occured """
            header = server_socket_makefile.readline(4096) # Max limit on readline, should likely not be exceeded
            if not header:
                raise ConnectionError # We should always be able to read the header, otherwise connection failure

            # Attempt to process the header
            try:
                metadata = json.loads(header.decode("utf-8").strip()) # Strip should remove the newline character
            except json.JSONDecodeError:
                # Malformed header, don't attempt to process it further
                print("Unable to read response from server: Malformed Header")
                return None
            except Exception as e:
                # Other error, don't attempt to deal with it further
                print(f"Error parsing json: {e}")
                return None
            return metadata
    
def clear_buffer(server_socket, server_socket_makefile):
    original_timeout_setting = server_socket.gettimeout()
    server_socket.setblocking(False)

    try:
        while True:
            data = server_socket_makefile.read(4096)
            if not data:
                # Connection closed
                break
    except BlockingIOError:
        # Buffer should be empty
        pass
    finally:
        server_socket.settimeout(original_timeout_setting) # Re-enables blocking

