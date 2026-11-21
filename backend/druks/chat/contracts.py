from druks.instructions import InstructionOutput


class AnswerOutput(InstructionOutput):
    # What an agent returns for a task that a person gave in a chat.
    answer: str

    def to_result(self) -> str:
        return self.answer
