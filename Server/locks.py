import os
import threading

class FileLock:
    """Using binary semaphores for writer and reader locks"""

    # Basic design is pretty simple, one read count with a mutex (binary sempahore)
    # One mutex lock for the file access itself for a writer (binary semaphore)
    # One mutex lock for the section

    # First reader acquire the mutex lock for the writer file access
    # Readers acquire the mutex lock for the section, grab the mutex lock for the counter, increment it
    # Release the mutex lock for the counter and the section and proceed into the file
    # When readers exit, they reaquire the mutex lock for the counter and decrement it
    # Last reader releases the mutex lock for the for the writers file access

    # Writers can grab the sectional mutex lock to prevent new readers from entering, and then wait on the writer lock.

    # Waked threads via seamphore is a bit random but it should work well enough for this purpose
    # It doesn't seem worth it for the limited amount of parallelism that happens to implement more memeory intensive solutions to fix that

    def __init__(self):
        self.readCount = 0 
        self.retired = False # Marks if this lock has been retired.
        # A retired lock is one marked for deletion, but exists so that any remaining threads waiting on section lock 
        # Retired is attached to the counter lock for safety
        # or otherwise can safely exit

        # Create binary semaphores
        self.sectionLock = threading.Semaphore(1) #binary semaphore
        self.counterLock = threading.Semaphore(1) #binary semaphore
        self.writerLock = threading.Semaphore(1) #binary semaphore

    def acquire_read(self) -> bool:
        self.sectionLock.acquire() # Acquire the section lock 
        self.counterLock.acquire() # Acquire the counter lock
        if (self.retired == True): 
            # This lock has been decomissoned, cannot acquire read. Release locks
            self.counterLock.release() 
            self.sectionLock.release() 
            return False
        if (self.readCount == 0):
            self.writerLock.acquire() # Acquire the writer lock if this is the first reader
            self.readCount = 1
        else:
            self.readCount += 1 # Increment the read counter

        # Release the counter and section locks and proceed
        self.counterLock.release() # Release the counter lock to allow other reading threads to enter or leave
        self.sectionLock.release() # Release the section lock to allow new threads to enter
        return True

    def acquire_write(self) -> bool:
        self.sectionLock.acquire() # Acquire the section lock
        self.writerLock.acquire() # Acquire the writerLock
        # If the lock has been retired, release internal locks and return false
        # Else return true
        if (self.retired == True):
            self.sectionLock.release()
            self.writerLock.release() 
            return False
        else:
            return True
        # DO NOT RELEASE THE SECTION LOCK OR WRITER LOCK UNTILL WRITER IS FINISHED,

    def release_read(self) -> bool:
        self.counterLock.acquire() # Acquire the counter lock
        if (self.readCount == 0): # Prevent errors that occur during adding a reader to prevent releasing one
            self.counterLock.release() # Release the counter lock
            return False # Return false to indicate there was no read to release
        if (self.readCount == 1):
            self.writerLock.release() # Release the writer lock if this is the last reader
            self.readCount = 0
        else:
            self.readCount -= 1 # Decrement the read count
        self.counterLock.release() # Release the counter locks to allow other reading threads to enter or leave
        return True # Return true if successful

    def release_write(self):
        self.writerLock.release() # Release the writer lock before new threads enter to make sure all works as intended
        self.sectionLock.release() # Release the section lock to allow new threads to enter

from contextlib import contextmanager # Unsure if this is the best way to handle the rest of this code

class LockManager:
    """ This manager acts as the central lookup for all the filelocks. Mutex lock + dictionary of overall locks based on file path """

    def __init__(self): #folder_path: str
        self.locks: dict[str, FileLock] = {} # Initalize an empty dictionary of Filelocks that use the filepath as an input
        self.lock = threading.Lock() # Mutex lock to protect datastructure, ownership is important in this case so we use a lock
        # over a binary semaphore

    def buildOrAcquireLock(self, filepath):
        """ Return the lock associated with a particular file. Create one if it does not eixst. """
        # Normalize path to the absolute path so relative paths do not generate new file locks
        normalized_path = os.path.abspath(filepath)

        with self.lock:
            # use with lock for safety for errors, we don't use this earlier because we have to hold a lock inbetween and
            # not just in one function call.
            file_lock = self.locks.get(normalized_path) # grab the value associated with the absolute file path
            if file_lock is not None:
                # the file_lock exists
                return file_lock
            else:
                file_lock = FileLock() # Generate a new instance of the filelock to represent this object
                self.locks[normalized_path] = file_lock # Save it to the dictionary
                return file_lock

    def deleteLock(self, filepath):
        """Delete the lock associated with a particular file. CALLING FUNCTION MUST HOLD THE SECTION LOCK
        AND WRITER LOCK."""
        # Normalize path to the absolute path so relative paths do not cause issues with deletion
        normalized_path = os.path.abspath(filepath)

        with self.lock:
            # use with lock for safety
            file_lock = self.locks.get(normalized_path)
            file_lock.retired = True # Retire the lock
            del self.locks[normalized_path] # Remove both value and key

        # Old code commented out

        # with self.lock:
        #     # Use with lock for safety
        #     file_lock = self.locks.get(normalized_path)

        #     if file_lock.sectionLock.acquire(blocking=False): # Attempt to nonblocking acquire the sectionlock
        #         if file_lock.writerLock.acquire(blocking=False): # Attempt to nonblocking acquire the writerLock
        #             if file_lock.counterLock.acquire(blocking=False): # Attempt to nonblocking acquire the counterLock
        #                 # We are good to delete the file
        #                 del self.locks[normalized_path] # Remove the object and key from the dictionary
        #             else:
        #                 # Release the acquire locks and return false
        #                 file_lock.sectionLock.release()
        #                 file_lock.writerLock.release()
        #                 return False
        #         else:
        #             # Release the acquired locks and return false
        #             file_lock.sectionLock.release()
        #             return False
        #     else:
        #         return False