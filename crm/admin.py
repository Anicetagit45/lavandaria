from django.contrib import admin
from django.urls import path
from .views import crm_pos_venda


def get_admin_urls(urls):
    def get_urls():
        custom_urls = [
            path(
                "crm-pos-venda/",
                admin.site.admin_view(crm_pos_venda),
                name="crm-pos-venda",
            ),
        ]
        return custom_urls + urls

    return get_urls


admin.site.get_urls = get_admin_urls(admin.site.get_urls())
