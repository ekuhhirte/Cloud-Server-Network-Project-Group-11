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

        # Create binary semaphores
        self.sectionLock = threading.Semaphore(1) #binary semaphore
        self.counterLock = threading.Seamphore(1) #binary semaphore
        self.writerLock = threading.Seamphore(1) #binary semaphore

    def acquire_read(self):
        self.sectionLock.acquire() # Acquire the section lock 
        self.counterLock.acquire() # Acquire the counter lock
        if (self.readCount == 0):
            self.writerLock.acquire() # Acquire the writer lock if this is the first reader
            self.readCount = 1
        else:
            self.readCount += 1 # Increment the read counter

        # Release the counter and section locks and proceed
        self.counterLock.release() # Release the counter lock to allow other reading threads to enter or leave
        self.sectionLock.release() # Release the section lock to allow new threads to enter

    def acquire_write(self):
        self.sectionLock.acquire() # Acquire the section lock
        self.writerLock.acquire() # Acquire the writerLock
        # DO NOT RELEASE THE SECTION LOCK OR WRITER LOCK UNTILL WRITER IS FINISHED

    def release_read(self):
        self.counterLock.acquire() # Acquire the counter lock
        if (self.readCount == 1):
            self.writerLock.release() # Release the writer lock if this is the last reader
            self.readCount = 0
        else:
            self.readCount -= 1 # Decrement the read count
        self.counterLock.release() # Release the counter locks to allow other reading threads to enter or leave

    def release_write(self):
        self.writerLock.release() # Release the writer lock before new threads enter to make sure all works as intended
        self.sectionLock.release() # Release the section lock to allow new threads to enter

from contextlib import contextmanager # Unsure if this is the best way to handle the rest of this code

class LockManager:
    """ This manager acts as the central lookup for all the filelocks. Mutex lock + dictionary of overall locks based on file path """

    def __init__(self, folder_path: str):
        