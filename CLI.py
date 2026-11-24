#ok this is the command line so it needs to tell you commands wow incredible im so smart
'''
Client CLI and Anaylsis:
Build the basic client command line interface and 
build the analysis module that takes the saved raw data
and convert it to overall statistics.
'''
import getpass as pw #hides password on the command line

#ok welcome message i guess
print("Welcome. Please enter your login information.")
username = input("Username:")
password = hash(pw.getpass("Password:")) 
#temp hash with built in python hash func - change when real hash function made (or different encryption method)


      
