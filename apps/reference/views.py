from django.views.generic import TemplateView


class ReferenceHomeView(TemplateView):
    template_name = "reference/index.html"
