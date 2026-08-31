class FileError(Exception):
    pass


class FileUnavailableError(FileError):
    pass


class FileTooLargeError(FileError):
    pass
