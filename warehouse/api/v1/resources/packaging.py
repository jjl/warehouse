from django.conf.urls import url
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from tastypie import fields
from tastypie.authentication import MultiAuthentication
from tastypie.authorization import DjangoAuthorization
from tastypie.bundle import Bundle
from tastypie.constants import ALL, ALL_WITH_RELATIONS
from tastypie.exceptions import NotFound
from tastypie.resources import ModelResource as TastypieModelResource
from tastypie.utils import trailing_slash

from warehouse.api.authentication import BasicAuthentication
from warehouse.api.fields import Base64FileField
from warehouse.api.resources import ModelResource
from warehouse.api.serializers import Serializer
from warehouse.api.validation import FormValidation
from warehouse.api.v1.forms.packaging import ProjectForm, VersionForm, VersionFileForm
from warehouse.models import Event
from warehouse.models import Project, Version, VersionFile, Classifier
from warehouse.models import Require, Provide, Obsolete


# @@@ Sort out Permissions
# @@@ Allow deletion of projects if user has permission
# @@@ Allow creating a new version of a project if user has permission
# @@@ Allow editing a version of a project if user has permission
#         - Should restrict which fields can be edited based on user
# @@@ Hydrate classifiers into Trove objects
#         - We should not allow new Trove objects to be created
# @@@ Fix BasicAuthentication Realm


__all__ = [
        "ProjectResource", "VersionResource", "FileResource",
        "RequireResource", "ObsoleteResource", "ProvideResource",
    ]


def handle_yanked_versions(bundle):
    if not bundle.request.GET.get("show_yanked", "no").lower() in ["yes", "on", "true", "t", "1"]:
        return bundle.obj.versions.filter(yanked=False).order_by("-order")
    return bundle.obj.versions.all().order_by("-order")


def handle_one_yanked_versions(bundle):
    qs = handle_yanked_versions(bundle)[:1]
    try:
        return qs.get()
    except qs.model.DoesNotExist:
        return None


def handle_yanked_files(bundle):
    if not bundle.request.GET.get("show_yanked", "no").lower() in ["yes", "on", "true", "t", "1"]:
        return bundle.obj.files.filter(yanked=False)
    return bundle.obj.files.all()


class ProjectResource(ModelResource):

    # Read only fields
    downloads = fields.IntegerField(attribute="downloads", readonly=True)
    normalized = fields.CharField(attribute="normalized", readonly=True)

    # related fields
    versions = fields.ToManyField("warehouse.api.v1.resources.VersionResource", handle_yanked_versions, readonly=True, null=True)
    latest = fields.ToOneField("warehouse.api.v1.resources.VersionResource", handle_one_yanked_versions, readonly=True, null=True)

    class Meta:
        resource_name = "projects"
        detail_uri_name = "normalized"

        queryset = Project.objects.all()
        fields = ["created", "downloads", "name", "normalized"]

        filtering = {
            "name": ALL,
            "normalized": ALL,
        }

        ordering = ["name", "normalized", "downloads"]

        authentication = MultiAuthentication(BasicAuthentication())
        authorization = DjangoAuthorization()

        list_allowed_methods = ["get", "post"]
        detail_allowed_methods = ["get", "put", "delete"]

        cache_control = {"public": True, "max_age": 60, "s_maxage": 60}

        serializer = Serializer(formats=["json", "jsonp"])
        validation = FormValidation(form_class=ProjectForm)

    def on_obj_create(self, obj, request=None, **kwargs):
        data = {}
        for field in obj._meta.fields:
            data[field.name] = getattr(obj, field.name)

        Event.objects.log(user=request.user, project=obj.name, action=Event.ACTIONS.project_created, data=data)

    def on_obj_update(self, old_obj, new_obj, request=None, **kwargs):
        data = {}
        for field in new_obj._meta.fields:
            data[field.name] = getattr(new_obj, field.name)

        Event.objects.log(user=request.user, project=new_obj.name, action=Event.ACTIONS.project_updated, data=data)

    def on_obj_delete(self, obj, request=None, **kwargs):
        data = {}
        for field in obj._meta.fields:
            data[field.name] = getattr(obj, field.name)

        Event.objects.log(user=request.user, project=obj.name, action=Event.ACTIONS.project_deleted, data=data)


class VersionResource(ModelResource):

    # Read only fields
    downloads = fields.IntegerField(attribute="downloads", readonly=True)
    project = fields.ToOneField("warehouse.api.v1.resources.ProjectResource", "project")
    version = fields.CharField(attribute="version")
    yanked = fields.BooleanField(attribute="yanked", readonly=True)

    # Advanced Data Prep
    uris = fields.DictField(attribute="uris")
    platforms = fields.ListField(attribute="platforms")
    supported_platforms = fields.ListField(attribute="supported_platforms")
    requires_external = fields.ListField(attribute="requires_external")
    keywords = fields.ListField(attribute="keywords")

    author = fields.DictField()
    maintainer = fields.DictField()
    classifiers = fields.ListField()

    # Related fields
    files = fields.ToManyField("warehouse.api.v1.resources.FileResource", handle_yanked_files, readonly=True, null=True)

    requires = fields.ToManyField("warehouse.api.v1.resources.RequireResource", "requires", related_name="project_version", null=True, full=True)
    provides = fields.ToManyField("warehouse.api.v1.resources.ProvideResource", "provides", related_name="project_version", null=True, full=True)
    obsoletes = fields.ToManyField("warehouse.api.v1.resources.ObsoleteResource", "obsoletes", related_name="project_version", null=True, full=True)

    class Meta:
        resource_name = "versions"
        detail_uri_name = "version"

        queryset = Version.objects.all()
        fields = [
            "created", "project", "version", "yanked",
            "summary", "description", "license", "uris",
            "author", "maintainer", "keywords",
            "platforms", "supported_platforms",
            "requires_python", "requires_external",
            "requires", "provides", "obsoletes", "downloads",
        ]

        filtering = {
            "project": ALL_WITH_RELATIONS,
            "version": ALL_WITH_RELATIONS,
        }

        authentication = MultiAuthentication(BasicAuthentication())
        authorization = DjangoAuthorization()

        list_allowed_methods = ["get", "post"]
        detail_allowed_methods = ["get", "put", "delete"]

        cache_control = {"public": True, "max_age": 60, "s_maxage": 60}

        serializer = Serializer(formats=["json", "jsonp"])
        validation = FormValidation(form_class=VersionForm)

    def base_urls(self):
        return [
            url(r"^(?P<resource_name>%s)%s$" % (self._meta.resource_name, trailing_slash()), self.wrap_view('dispatch_list'), name="api_dispatch_list"),
            url(r"^(?P<resource_name>%s)/(?P<project__normalized>[^/]+)/(?P<%s>[^/]+)%s$" % (self._meta.resource_name, self._meta.detail_uri_name, trailing_slash()), self.wrap_view('dispatch_detail'), name="api_dispatch_detail"),
        ]

    def detail_uri_kwargs(self, bundle_or_obj):
        obj = bundle_or_obj.obj if isinstance(bundle_or_obj, Bundle) else bundle_or_obj

        uri_kwargs = super(ModelResource, self).detail_uri_kwargs(bundle_or_obj)
        uri_kwargs.update({
            "project__normalized": obj.project.normalized,
        })

        return uri_kwargs

    def build_filters(self, filters=None):
        if filters is None:
            filters = {}

        orm_filters = super(VersionResource, self).build_filters(filters)

        if not filters.get("show_yanked", "no").lower() in ["yes", "on", "true", "t", "1"]:
            orm_filters["yanked"] = False

        return orm_filters

    def dehydrate_author(self, bundle):
        person = {}

        if bundle.obj.author:
            person.update({"name": bundle.obj.author})

        if bundle.obj.author_email:
            person.update({"email": bundle.obj.author_email})

        return person

    def dehydrate_maintainer(self, bundle):
        person = {}

        if bundle.obj.maintainer:
            person.update({"name": bundle.obj.maintainer})

        if bundle.obj.maintainer_email:
            person.update({"email": bundle.obj.maintainer_email})

        return person

    def dehydrate_classifiers(self, bundle):
        return [c.trove for c in bundle.obj.classifiers.all().order_by("trove")]

    def hydrate(self, bundle):
        if bundle.obj.yanked:
            bundle.obj.yanked = False

        bundle.obj.author = bundle.data.get("author", {}).get("name", "")
        bundle.obj.author_email = bundle.data.get("author", {}).get("email", "")

        bundle.obj.maintainer = bundle.data.get("maintainer", {}).get("name", "")
        bundle.obj.maintainer_email = bundle.data.get("maintainer", {}).get("email", "")

        return bundle

    def _fix_tastypie_m2m_bug(self, bundle, key, field):
        data = []

        for x in bundle.data.get(key, []):
            if not isinstance(x, Bundle):
                _data = x.copy()
                _data.update({
                        field: bundle.obj,
                    })
                data.append(_data)

        if data:
            bundle.data[key] = data

        return bundle

    def hydrate_requires(self, bundle):
        return self._fix_tastypie_m2m_bug(bundle, "requires", "project_version")

    def hydrate_provides(self, bundle):
        return self._fix_tastypie_m2m_bug(bundle, "provides", "project_version")

    def hydrate_obsoletes(self, bundle):
        return self._fix_tastypie_m2m_bug(bundle, "obsoletes", "project_version")

    def save_m2m(self, bundle):
        classifiers = bundle.data.get("classifiers", [])
        if classifiers is not None:
            # @@@ This needs permissions to go with it
            classifier_objects = [Classifier.objects.get_or_create(trove=c) for c in classifiers]
            bundle.obj.classifiers.add(*[x[0] for x in classifier_objects])

        super(VersionResource, self).save_m2m(bundle)

    def obj_delete(self, request=None, **kwargs):
        obj = kwargs.pop("_obj", None)

        if not getattr(obj, "pk", None):
            try:
                obj = self.obj_get(request, **kwargs)
            except ObjectDoesNotExist:
                raise NotFound("A model instance matching the provided arguments could not be found.")

        with transaction.commit_on_success():
            obj.yanked = True
            obj.save()

            self.on_obj_delete(obj, request=request, **kwargs)

            obj.files.update(yanked=True)

            for f in obj.files.all():
                data = {}
                for field in f._meta.fields:
                    if field.name in set(["version"]):
                        continue

                    if field.name == "file":
                        data[field.name] = f.file.url
                    else:
                        data[field.name] = getattr(f, field.name)

                Event.objects.log(
                                user=request.user,
                                project=obj.project.name, version=obj.version, filename=f.filename,
                                action=Event.ACTIONS.file_deleted,
                                data=data
                            )

    def on_obj_create(self, obj, request=None, **kwargs):
        data = {}
        for field in obj._meta.fields:
            if field.name in set(["project"]):
                continue

            data[field.name] = getattr(obj, field.name)

        Event.objects.log(user=request.user, project=obj.project.name, version=obj.version, action=Event.ACTIONS.version_created, data=data)

    def on_obj_update(self, old_obj, new_obj, request=None, **kwargs):
        if old_obj.yanked and not new_obj.yanked:
            self.on_obj_create(new_obj, request=request, **kwargs)
        else:
            data = {}
            for field in new_obj._meta.fields:
                if field.name in set(["project"]):
                    continue

                data[field.name] = getattr(new_obj, field.name)

            Event.objects.log(user=request.user, project=new_obj.project.name, version=new_obj.version, action=Event.ACTIONS.version_updated, data=data)

    def on_obj_delete(self, obj, request=None, **kwargs):
        data = {}
        for field in obj._meta.fields:
            if field.name in set(["project"]):
                continue

            data[field.name] = getattr(obj, field.name)

        Event.objects.log(user=request.user, project=obj.project.name, version=obj.version, action=Event.ACTIONS.version_deleted, data=data)


class RequireResource(TastypieModelResource):

    # Related fields
    project_version = fields.ToOneField("warehouse.api.v1.resources.VersionResource", "project_version")

    class Meta:
        fields = ["name", "version", "environment"]
        include_resource_uri = False
        queryset = Require.objects.all()

        serializer = Serializer(formats=["json", "jsonp"])

    def dehydrate(self, bundle):
        if "project_version" in bundle.data:
            del bundle.data["project_version"]
        return bundle


class ProvideResource(TastypieModelResource):

    # Related fields
    project_version = fields.ToOneField("warehouse.api.v1.resources.VersionResource", "project_version")

    class Meta:
        fields = ["name", "version", "environment"]
        include_resource_uri = False
        queryset = Provide.objects.all()

        serializer = Serializer(formats=["json", "jsonp"])

    def dehydrate(self, bundle):
        if "project_version" in bundle.data:
            del bundle.data["project_version"]
        return bundle


class ObsoleteResource(TastypieModelResource):

    # Related fields
    project_version = fields.ToOneField("warehouse.api.v1.resources.VersionResource", "project_version")

    class Meta:
        fields = ["name", "version", "environment"]
        include_resource_uri = False
        queryset = Obsolete.objects.all()

        serializer = Serializer(formats=["json", "jsonp"])

    def dehydrate(self, bundle):
        if "project_version" in bundle.data:
            del bundle.data["project_version"]
        return bundle


class FileResource(ModelResource):

    # Read only fields
    modified = fields.DateTimeField(attribute="modified", readonly=True)
    downloads = fields.IntegerField(attribute="downloads", readonly=True)
    digests = fields.DictField(attribute="digests", readonly=True)
    filename = fields.CharField(attribute="filename", readonly=True)
    filesize = fields.IntegerField(attribute="filesize", readonly=True)

    # Related fields
    version = fields.ToOneField("warehouse.api.v1.resources.VersionResource", "version")

    # Advanced data prep fields
    file = Base64FileField(attribute="file")

    class Meta:
        resource_name = "files"
        detail_uri_name = "filename"

        queryset = VersionFile.objects.all()
        fields = [
            "version", "created", "modified", "yanked", "type", "file",
            "python_version", "digests", "comment", "filename", "filesize",
            "downloads",
        ]

        filtering = {
            "version": ALL_WITH_RELATIONS,
        }

        authentication = MultiAuthentication(BasicAuthentication())
        authorization = DjangoAuthorization()

        list_allowed_methods = ["get", "post"]
        detail_allowed_methods = ["get", "put", "delete"]

        cache_control = {"public": True, "max_age": 60, "s_maxage": 60}

        serializer = Serializer(formats=["json", "jsonp"])
        validation = FormValidation(form_class=VersionFileForm)

    def build_filters(self, filters=None):
        if filters is None:
            filters = {}

        orm_filters = super(FileResource, self).build_filters(filters)

        if not filters.get("show_yanked", "no").lower() in ["yes", "on", "true", "t", "1"]:
            orm_filters["yanked"] = False

        return orm_filters

    def hydrate(self, bundle):
        if bundle.obj.yanked:
            bundle.obj.yanked = False

        return bundle

    def obj_delete(self, request=None, **kwargs):
        obj = kwargs.pop("_obj", None)

        if not getattr(obj, "pk", None):
            try:
                obj = self.obj_get(request, **kwargs)
            except ObjectDoesNotExist:
                raise NotFound("A model instance matching the provided arguments could not be found.")

        with transaction.commit_on_success():
            obj.yanked = True
            obj.save()

            self.on_obj_delete(obj, request=request, **kwargs)

    def on_obj_create(self, obj, request=None, **kwargs):
        data = {}
        for field in obj._meta.fields:
            if field.name in set(["version"]):
                continue

            if field.name == "file":
                data[field.name] = obj.file.url
            else:
                data[field.name] = getattr(obj, field.name)

        Event.objects.log(
                        user=request.user,
                        project=obj.version.project.name, version=obj.version.version, filename=obj.filename,
                        action=Event.ACTIONS.file_created,
                        data=data
                    )

    def on_obj_update(self, old_obj, new_obj, request=None, **kwargs):
        if old_obj.yanked and not new_obj.yanked:
            self.on_obj_create(new_obj, request=request, **kwargs)
        else:
            data = {}
            for field in new_obj._meta.fields:
                if field.name in set(["version"]):
                    continue

                if field.name == "file":
                    data[field.name] = new_obj.file.url
                else:
                    data[field.name] = getattr(new_obj, field.name)

            Event.objects.log(
                            user=request.user,
                            project=new_obj.version.project.name, version=new_obj.version.version, filename=new_obj.filename,
                            action=Event.ACTIONS.file_updated,
                            data=data
                        )

    def on_obj_delete(self, obj, request=None, **kwargs):
        data = {}
        for field in obj._meta.fields:
            if field.name in set(["version"]):
                continue

            if field.name == "file":
                data[field.name] = obj.file.url
            else:
                data[field.name] = getattr(obj, field.name)

        Event.objects.log(
                        user=request.user,
                        project=obj.version.project.name, version=obj.version.version, filename=obj.filename,
                        action=Event.ACTIONS.file_deleted,
                        data=data
                    )
