import copy
import hashlib
import urllib

from django.conf.urls import url
from django.core.exceptions import ObjectDoesNotExist
from django.core.signals import got_request_exception
from django.db import transaction
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import condition

from tastypie.exceptions import NotFound, ImmediateHttpResponse
from tastypie.resources import ModelResource as TastypieModelResource
from tastypie.utils import trailing_slash
from tastypie.utils.mime import build_content_type

from warehouse.api.http import HttpUnprocessableEntity


__all__ = ["ModelResource"]


class CleanErrors(object):

    def wrap_view(self, view):
        @csrf_exempt
        def wrapper(request, *args, **kwargs):
            resp = super(CleanErrors, self).wrap_view(view)(request, *args, **kwargs)

            if resp.status_code == 400:
                if request:
                    desired_format = self.determine_format(request)
                else:
                    desired_format = self._meta.default_format

                resp.content = self.serialize(request, {"message": resp.content}, desired_format)
                resp["Content-Type"] = build_content_type(desired_format)

            return resp

        return wrapper

    def error_response(self, errors, request):
        if request:
            desired_format = self.determine_format(request)
        else:
            desired_format = self._meta.default_format

        serialized = self.serialize(request, errors, desired_format)
        response = HttpUnprocessableEntity(content=serialized, content_type=build_content_type(desired_format))
        raise ImmediateHttpResponse(response=response)

    def is_valid(self, bundle, request=None):
        """
        Handles checking if the data provided by the user is valid.

        Mostly a hook, this uses class assigned to ``validation`` from
        ``Resource._meta``.

        If validation fails, an error is raised with the error messages
        serialized inside it.
        """
        errors = self._meta.validation.is_valid(bundle, request)

        if errors:
            bundle.errors["message"] = "Validation Error"
            bundle.errors["errors"] = [dict(resource=self._meta.resource_name, **e) for e in errors]
            return False

        return True


class ClientCache(object):
    def create_response(self, request, data, **response_kwargs):
        response = super(ClientCache, self).create_response(request, data, **response_kwargs)

        if request.method == "GET" and response.status_code == 200 and hasattr(self._meta, "cache_control"):
            cache_control = self._meta.cache_control.copy()
            patch_cache_control(response, **cache_control)

        patch_vary_headers(response, ["Accept"])

        return response


class Conditional(object):

    def wrap_view(self, view):

        def lmodified(request, *args, **kwargs):
            lookup_kwargs = self.remove_api_resource_names(kwargs)

            if lookup_kwargs:
                try:
                    obj = self.obj_get(request=request, **lookup_kwargs)
                except self._meta.queryset.model.DoesNotExist:
                    return

                if hasattr(obj, "modified"):
                    return obj.modified

        def etag(request, *args, **kwargs):
            last_modified = lmodified(request, *args, **kwargs)

            if last_modified is not None:
                return hashlib.sha224(last_modified.isoformat()).hexdigest()

        @csrf_exempt
        @condition(etag_func=etag, last_modified_func=lmodified)
        def wrapper(request, *args, **kwargs):
            return super(Conditional, self).wrap_view(view)(request, *args, **kwargs)

        return wrapper


class FixExceptionHandling(object):

    def _handle_500(self, request, exception):
        got_request_exception.send(sender=self.__class__, request=request)
        return super(FixExceptionHandling, self)._handle_500(request, exception)


class ModelResource(CleanErrors, ClientCache, Conditional, FixExceptionHandling, TastypieModelResource):

    def filter_value_to_python(self, value, field_name, filters, filter_expr,
            filter_type):
        """
        Turn the string ``value`` into a python object.
        """
        if self.fields[field_name].dehydrated_type == "string":
            value = value
        elif value in ["true", "True", True]:
            value = True
        elif value in ["false", "False", False]:
            value = False
        elif value in ["nil", "none", "None", None]:
            value = None

        # Split on ',' if not empty string and either an in or range filter.
        if filter_type in ('in', 'range') and len(value):
            if hasattr(filters, 'getlist'):
                value = []

                for part in filters.getlist(filter_expr):
                    value.extend(part.split(','))
            else:
                value = value.split(',')

        return value

    def base_urls(self):
        """
        The standard URLs this ``Resource`` should respond to.
        """
        return [
            url(r"^(?P<resource_name>%s)%s$" % (self._meta.resource_name, trailing_slash()), self.wrap_view('dispatch_list'), name="api_dispatch_list"),
            url(r"^(?P<resource_name>%s)/(?P<%s>[^/]+)%s$" % (self._meta.resource_name, self._meta.detail_uri_name, trailing_slash()), self.wrap_view('dispatch_detail'), name="api_dispatch_detail"),
        ]

    def get_via_uri(self, uri, request=None):
        # @@@ Hackish
        uri = urllib.unquote(uri)
        return super(ModelResource, self).get_via_uri(uri, request=request)

    def obj_create(self, bundle, request=None, **kwargs):
        with transaction.commit_on_success():
            bundle = super(ModelResource, self).obj_create(bundle, request=request, **kwargs)

            try:
                self.on_obj_create(bundle.obj, request=request, **kwargs)
            except NotImplementedError:
                pass

        return bundle

    def on_obj_create(self, obj, request=None, **kwargs):
        raise NotImplementedError()

    def obj_update(self, bundle, request=None, skip_errors=False, **kwargs):
        with transaction.commit_on_success():
            # Hack to force lookup
            bundle.obj = self.obj_get(request=request, **kwargs)
            current = copy.copy(bundle.obj)

            bundle = super(ModelResource, self).obj_update(bundle, request=request, skip_errors=skip_errors, **kwargs)

            try:
                self.on_obj_update(current, bundle.obj, request=request, **kwargs)
            except NotImplementedError:
                pass

        return bundle

    def on_obj_update(self, old_obj, new_obj, request=None, **kwargs):
        raise NotImplementedError()

    def obj_delete(self, request=None, **kwargs):
        obj = kwargs.pop("_obj", None)

        if obj is None:
            try:
                obj = self.obj_get(request, **kwargs)
            except ObjectDoesNotExist:
                raise NotFound("A model instance matching the provided arguments could not be found.")

        kwargs["_obj"] = obj

        with transaction.commit_on_success():
            try:
                self.on_obj_delete(obj, request=request, **kwargs)
            except NotImplementedError:
                pass

            return super(ModelResource, self).obj_delete(request=request, **kwargs)

    def on_obj_delete(self, obj, request=None, **kwargs):
        raise NotImplementedError()
