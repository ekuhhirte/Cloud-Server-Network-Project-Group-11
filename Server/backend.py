import socket # for TCP
import threading # for threads
import json # for headers
import os # for file operations

def handle_client(client_socket, client_address):
    """This function is the basis of each thread, and it continues serving the client. 
    It checks the header of the incoming message, and runs the corresponding commands.
    It sends back responses messages as needed where the corresponding commands won't."""
    print(f"New connection from {client_address}")
    with client_socket:
        # using with for the auto close to save manual work
        with client_socket.makefile("rb") as sock_file:
            try:
                while True:
                    header = sock_file.readline(4096) #max limit on readline, should likely not be exceeded
                    if not header:
                        raise ConnectionError # we should always be able to read the header, otherwise connection failed
                    
                    # attempt to process the header, if it has an error return back an error message and wait for a new header
                    try:
                        metadata = json.loads(header.decode("utf-8").strip()) # strip should remove the newline character
                    except json.JSONDecodeError:
                        returnHeader = json.dumps({"type": "error", "error" : "Malformed Header"}) + "\n"
                        client_socket.sendall(returnHeader.encode("utf-8"))
                        continue # skip loop iteration and wait for a new header 
                    except Exception as e:
                        returnHeader = json.dumps({"type" : "error", "error" : "Internal Server Error"}) + "\n"
                        client_socket.sendall(returnHeader.encode("utf-8"))
                        continue # skip loop iteration and wait for a new header


                    # decode should convert from utf-8 to string, strip newline and parse as json
                    # While this isn't a super optimized stream reading approach, our headers should be small enough to
                    # avoid most issues with memory consumption.
                    message_type = metadata["type"]
                    try:
                        if (message_type == "Download Request"):
                            send_file_to_client(client_socket, metadata["file_path"])
                            # unsure if we want to build a handler function to check all conditionals
                        elif (message_type == "Upload Request"):
                            pass
                            #recieve_file_from_client(client_socket, ...)
                        else:
                            # unrecognized type, send back error message
                            returnHeader = json.dumps({"type" : "error", "error" : "Unknown Command", "command" : metadata["type"]}) + "\n"
                            client_socket.sendall(returnHeader.encode("utf-8"))
                    except KeyError:
                        # if we are unable to read any of the keys that we should expect, send back an error
                        returnHeader = json.dumps({"type" : "error", "error" : "Malformed Header"}) + "\n"
                        client_socket.sendall(returnHeader.encode("utf-8"))
            except ConnectionError as e:
                print(f"Error with connection: {e}")
                # Terminate connection or thread here
            except Exception as e:
                print(f"Error Occured: {e}")
                # Terminate connection or thread here

def send_file_to_client(client_socket, file_path):
    """This function reads a file from a file path and streams it back to the client"""
    try:
        file_size = os.path.getsize(file_path) # get the size of the file
        filename = os.path.basename(file_path) # get the name of the file
    except FileNotFoundError:
        returnHeader = json.dumps({"type" : "error", "error" : "File Not Found"}) + "\n"
        client_socket.sendall(returnHeader.encode("utf-8")) # sends back the error header
        return
    except Exception as e:
        returnHeader = json.dumps({"type" : "error", "error" : "Internal Server Error"}) + "\n"
        client_socket.sendall(returnHeader.encode("utf-8")) # sends back the error header
        return

    returnHeader = json.dumps({"type": "Download Response", "filename" : filename, "file_size" : file_size}) + "\n" 
    # pack a header that the reciving side has the metadata they need to complete the transaction, newline indicates end

    client_socket.sendall(returnHeader.encode("utf-8")) # sends the encoded header

    # Stream file over TCP connection
    with open(file_path, "rb") as f: # this will need to be replaced by the new version using the double semaphore lock
        client_socket.sendfile(f)

def recieve_file_from_client(client_socket, file_name, file_path, file_size):
    # not sure if name is needed seperately from file_path, later todo
    pass

def initalize_listener(host="127.0.0.1", port=8080):
    #create a TCP socket for the listener
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # makes sure that if we restart the server
    # that the socket can be reacquired rather than held onto by the system (sets socket to reuse address to true)

    #bind the socket to the host and port provided in the arguments
    server_socket.bind((host, port))

    # Start listening
    server_socket.listen()
    print(f"Server is listening on {host}:{port}")

    try:
        while True:
            #constantly attempt to accept a new connection, which should block the thread
            #so that it only wakes up and uses CPU instantly upon a new connection
            #and then goes back to waiting
            client_socket, client_address = server_socket.accept()

            #create a new thread using the handle client function as a base passed
            #the tuple of information it needs.
            client_thread = threading.Thread(target=handle_client,
                                                args=(client_socket, client_address)
                                                )

            #unsure if we want to set the threads to daemon so they close automatically,
            #more likely we want the threads to assess their own existance and if it should continue
            #having connections be killed halfway through could be bad

            #USE FOR DEBUGGING ONLY, THIS BELOW LINE MUST BE COMMENTED OUT FOR FULL USE
            client_thread.daemon = True

            client_thread.start()

            print(f"Active connections: {threading.active_count()-1}")

    except KeyboardInterrupt:
        print("Stopping server")
    except Exception as e:
        print(f"Error, stopping server: {e}")
    finally:
        server_socket.close()

if __name__ == "__main__":
    initalize_listener() # start the listener up