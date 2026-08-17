from druks.extensions import Extension


class Usage(Extension):
    name = "usage"
    icon = "gauge"
    description = "Harness usage metering — quota and spend per account."
    builtin = True
