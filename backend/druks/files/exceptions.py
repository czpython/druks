from druks.exceptions import DruksError


class FileError(DruksError):
    pass


class FileUnavailableError(FileError):
    pass


class FileTooLargeError(FileError):
    pass
