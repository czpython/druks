from druks.apps import App


class Usage(App):
    name = "usage"
    icon = "gauge"
    description = "Provider usage metering — quota and spend per account."
    builtin = True
