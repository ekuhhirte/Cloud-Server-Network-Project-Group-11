import socket # for TCP
import threading # for threads
import json # for headers
import os # for file operations
import locks # for file locking

lock_manager = locks.LockManager() # Initalize the lock manager object as a global that all threads can access



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
                            recieve_file_from_client(client_socket, metadata["file_name"], metadata["file_path"], metadata["file_size"])
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
                return
                # Terminate connection or thread here
            except Exception as e:
                print(f"Error Occured: {e}")
                return
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

    file_lock = lock_manager.buildOrAcquireLock(file_path) # Get the file lock from the lock manager
    try:
       file_lock.acquire_read() # Acquire a reading lock
        # Stream file over TCP connection
       with open(file_path, "rb") as f: # this will need to be replaced by the new version using the double semaphore lock
           client_socket.sendfile(f)
    except Exception:
        print("Error occured while streaming file to client")
    finally:
        file_lock.release_read() # Release the reading lock

def recieve_file_from_client(client_socket, file_name, file_path, file_size):
    # not sure if name is needed seperately from file_path, later todo
    # need to normalize file_path later
    if file_name is None or file_path is None or file_size is None:
        returnHeader = json.dumps({"type" : "error", "error" : "Malformed Header"}) + "\n"
        client_socket.sendall(returnHeader.encode("utf-8")) # sends back the error header
        return
    # Otherwise we have the data we need
    # While data remains, write in 4096 chunks up to a file_size

    remaining_bytes = file_size # not sure if its cleaner to just decrement the file size as we go
    with open(file_path, "wb") as file:
        while remaining_bytes > 0:
            chunk = client_socket.recv(min(4096, remaining_bytes)) # Should this be using the makefile instead? Might be safer to use the buffer
            if not chunk:
                #returnHeader = json.dumps({"type" : "error", "error" : "Internal Server Error"}) + "\n"
                #client_socket.sendall(returnHeader.encode("utf-8")) # sends back the error header
                raise ConnectionError(f"Connection closed during file transfer")
            file.write(chunk)
            remaining_bytes -= len(chunk)

    returnHeader = json.dumps({"type" : "Upload Response"}) + "\n"
    client_socket.sendall(returnHeader.encode("utf-8")) # Sends back the success via the Upload Response header
    return

def delete_file(client_socket, file_path) -> bool:
    """ This function attempts to delete a file, but will return an error if the file is being used or doesn't exist."""
    if (os.path.isfile(file_path) == False):
        # The file does not exist
        returnHeader = json.dumps({"type": "error", "error": "File does not exist"})
        client_socket.sendall(returnHeader.encode("utf-8")) # Send back the error header
        return False
    file_lock = lock_manager.buildOrAcquireLock(file_path) # Get the filelock assocaited with the filepath 

    if file_lock.sectionLock.acquire(blocking=False):
       # Section lock was acquired (no reason to proceed further to get writing lock if section lock was occupied)
       if file_lock.writerLock.acquire(blocking=False):
           # Writing lock was acquired, no other threads are currently using the file

           # Set retired to true so that new threads don't enter the retired lock (as file is being removed)
           file_lock.counterLock.acquire() # Acquire the counting lock (blocking as no other threads should be in the file)
           file_lock.retired = True 

           try:
               os.remove(file_path) # Remove the file
           except Exception as e:
               print(f"Error removing file: {e}")
               returnHeader = json.dumps({"type": "error", "error": "Internal Server Error"})
               client_socket.sendall(returnHeader.encode("utf-8")) # Send back the error header
               return False
       else:
           returnHeader = json.dumps({"type": "error", "error": "Unable to acquire writer lock, file is occupied"})
           client_socket.sendall(returnHeader.encode("utf-8")) # Send back the error header
           return False
    else:
        returnHeader = json.dumps({"type": "error", "error": "Unable to acquire section lock, file is occupied"})
        client_socket.sendall(returnHeader.encode("utf-8")) # Send back the error header
        return False
    # File was deleted and lock was retired
    return True




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