import socket
import threading

def handle_client(client_socket, client_address):
        """Placeholder function that will be run in its own thread when a client connects.
        This function will be filled in with the actions each individual thread needs to deal
        with."""
        print(f"New connection from {client_address}")
        pass

def initalize_listener(host="127.0.0.1", port=8080):
    #create a TCP socket for the listener
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

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