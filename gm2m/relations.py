from django.db.models.fields.related import add_lazy_relation
from django.db.models.signals import pre_delete
from django.db.models.fields import FieldDoesNotExist
from django.utils.functional import cached_property
from django.db.utils import DEFAULT_DB_ALIAS
from django.db.models import Q
from django.utils import six

from .compat import apps, checks, GenericForeignKey, ForeignObject, \
                    ForeignObjectRel, is_swapped, add_related_field, \
                    get_model_name, is_fake_model

from .models import create_gm2m_intermediary_model
from .managers import create_gm2m_related_manager
from .descriptors import GM2MRelatedDescriptor, ReverseGM2MRelatedDescriptor
from .deletion import *
from .signals import deleting
from .helpers import get_content_type


# default relation attributes
# we set that here as they are used to retrieve the GM2MRel attribute from
# a GM2MUnitRel (see GM2MUnitRel.__getattribute__ below)
# they are also used in GM2MField.deconstruct

# attributes whose values can be overriden by kwargs
REL_ATTRS = {
    'related_name': None,
    'related_query_name': None,
    'through': None,
    'through_fields': None,
    'db_constraint': True,
    'for_concrete_model': True,
    'on_delete': CASCADE,
}

# attributes whose values cannot be changed
REL_ATTRS_FIXED = {
    'multiple': True,
    'symmetrical': False,
    'parent_link': False,
    'limit_choices_to': {}
}

REL_ATTRS_NAMES = list(REL_ATTRS.keys()) + list(REL_ATTRS_FIXED.keys())


class GM2MRelation(ForeignObject):
    """
    A reverse relation for a GM2MField.
    Each related model has a GM2MRelation towards the source model
    """

    # copies GM2MField flags (as self.field will always be a GM2MField)
    many_to_many = True
    many_to_one = False
    one_to_many = False
    one_to_one = False

    concrete = False
    generate_reverse_relation = False  # only used in Django 1.7
    related_accessor_class = GM2MRelatedDescriptor

    def __init__(self, to, field, rel, **kwargs):
        self.field = field

        kwargs.update({
            'rel': rel,
            'blank': True,
            'editable': False,
            'serialize': False
        })

        super(GM2MRelation, self).__init__(to, from_fields=[field.name],
                                           to_fields=[], **kwargs)

    def contribute_to_class(self, cls, name, virtual_only=False):
        pass

    def get_accessor_name(self):
        return self.rel.related_name \
            or (get_model_name(self.field.model) + '_set')

    def bulk_related_objects(self, objs, using=DEFAULT_DB_ALIAS):
        """
        Return all objects related to objs
        The returned result will be passed to Collector.collect, so one should
        not use the deletion functions as such
        """

        through = self.field.rel.through
        base_mngr = through._base_manager.db_manager(using)

        on_delete = self.rel.on_delete

        if on_delete is not DO_NOTHING:
            # collect related objects
            field_names = through._meta._field_names
            q = Q()
            for obj in objs:
                # Convert each obj to (content_type, primary_key)
                q = q | Q(**{
                    field_names['tgt_ct']: get_content_type(obj),
                    field_names['tgt_fk']: obj.pk
                })
            qs = base_mngr.filter(q)

            if on_delete in (DO_NOTHING_SIGNAL, CASCADE_SIGNAL,
                             CASCADE_SIGNAL_VETO):
                results = deleting.send(sender=self.field,
                                        del_objs=objs, rel_objs=qs)

            if on_delete in (CASCADE, CASCADE_SIGNAL) \
            or on_delete is CASCADE_SIGNAL_VETO \
            and not any(r[1] for r in results):
                # if CASCADE must be called or if no receiver returned a veto
                # we return the qs for deletion
                # note that it is an homogeneous queryset (as Collector.collect
                # which is called afterwards only works with homogeneous
                # collections)
                return qs

        # do not delete anything by default
        empty_qs = base_mngr.none()
        empty_qs.query.set_empty()
        return empty_qs


class GM2MUnitRelBase(ForeignObjectRel):
    # this is a separate implementation from GM2M below for compatibility
    # reasons (see compat.add_related_field)

    def __init__(self, field, to):
        super(GM2MUnitRelBase, self).__init__(field, to)
        self.multiple = True

    def get_related_field(self):
        """
        Returns the field in the to object to which this relationship is tied
        (this is always the primary key on the target model).
        """
        return self.to._meta.pk


class GM2MUnitRel(GM2MUnitRelBase):

    dummy_pre_delete = lambda s, **kwargs: None

    def check(self, **kwargs):
        errors = []
        errors.extend(self._check_referencing_to_swapped_model())
        errors.extend(self._check_clashes())
        return errors

    def _check_referencing_to_swapped_model(self):
        if self.to not in apps.get_models() \
        and not isinstance(self.to, six.string_types) \
        and self.to._meta.swapped:
            model = '%s.%s' % (
                self.to._meta.app_label,
                self.to._meta.object_name
            )
            return [checks.Error(
                ("Field defines a relation with the model '%s', "
                 "which has been swapped out.") % model,
                hint="Update the relation to point at 'settings.%s'."
                     % self.rel.to._meta.swappable,
                obj=self,
                id='gm2m.E201',
            )]
        return []

    def _check_clashes(self):
        """ Check accessor and reverse query name clashes. """

        from django.db.models.base import ModelBase

        errors = []
        opts = self.field.model._meta

        # `self.to` may be a string instead of a model. Skip if model name
        # is not resolved.
        if not isinstance(self.to, ModelBase):
            return []

        # If the field doesn't install backward relation on the target
        # model (so `is_hidden` returns True), then there are no clashes to
        # check and we can skip these fields.
        if self.is_hidden():
            return []

        try:
            self.related
        except AttributeError:
            return []

        # Consider that we are checking field `Model.gm2m` and the
        # models are:
        #
        #     class Target(models.Model):
        #         model = models.IntegerField()
        #         model_set = models.IntegerField()
        #
        #     class Model(models.Model):
        #         foreign = models.ForeignKey(Target)
        #         gm2m = GM2MField(Target)

        rel_opts = self.to._meta
        # rel_opts.object_name == "Target"
        rel_name = self.related.get_accessor_name()  # i. e. "model_set"
        rel_query_name = self.field.related_query_name()  # i. e. "model"
        field_name = "%s.%s" % (opts.object_name,
                                self.field.name)  # i. e. "Model.gm2m"

        # Check clashes between accessor or reverse query name of `field`
        # and any other field name -- i. e. accessor for Model.gm2m is
        # model_set and it clashes with Target.model_set.
        potential_clashes = rel_opts.fields + rel_opts.many_to_many
        for clash_field in potential_clashes:
            clash_name = "%s.%s" % (rel_opts.object_name,
                clash_field.name)  # i. e. "Target.model_set"
            if clash_field.name == rel_name:
                errors.append(
                    checks.Error(
                        "Reverse accessor for '%s' clashes with field "
                        "name '%s'." % (field_name, clash_name),
                        hint="Rename field '%s', or add/change a "
                             "related_name argument to the definition "
                             "for field '%s'." % (clash_name, field_name),
                        obj=self,
                        id='gm2m.E202',
                    )
                )

            if clash_field.name == rel_query_name:
                errors.append(
                    checks.Error(
                        "Reverse query name for '%s' clashes with field "
                        "name '%s'." % (field_name, clash_name),
                        hint="Rename field '%s', or add/change a "
                             "related_name argument to the definition "
                             "for field '%s'." % (clash_name, field_name),
                        obj=self,
                        id='gm2m.E203',
                    )
                )

        # Check clashes between accessors/reverse query names of `field`
        # and any other field accessor -- i. e. Model.gm2m accessor clashes
        # with Model.foreign accessor.
        potential_clashes = rel_opts.get_all_related_many_to_many_objects()
        potential_clashes += rel_opts.get_all_related_objects()
        potential_clashes = (r for r in potential_clashes
            if r.field is not self.field)
        for clash_field in potential_clashes:
            # "Model.gm2m"
            clash_name = "%s.%s" % (
                clash_field.model._meta.object_name,
                clash_field.field.name)
            if clash_field.get_accessor_name() == rel_name:
                errors.append(
                    checks.Error(
                        "Reverse accessor for '%s' clashes with reverse "
                        "accessor for '%s'." % (field_name, clash_name),
                        hint=("Add or change a related_name argument "
                              "to the definition for '%s' or '%s'.")
                             % (field_name, clash_name),
                        obj=self,
                        id='gm2m.E204',
                    )
                )

            if clash_field.get_accessor_name() == rel_query_name:
                errors.append(
                    checks.Error(
                        "Reverse query name for '%s' clashes with reverse "
                        "query name for '%s'." % (field_name, clash_name),
                        hint=("Add or change a related_name argument "
                              "to the definition for '%s' or '%s'.")
                             % (field_name, clash_name),
                        obj=self,
                        id='gm2m.E205',
                    )
                )

        return errors

    def __getattribute__(self, name):
        """
        General attributes are those from the GM2MRel object
        """
        sup = super(GM2MUnitRel, self).__getattribute__
        if name in REL_ATTRS_NAMES:
            if name == 'on_delete':
                name += '_tgt'
            return getattr(sup('field').rel, name)
        else:
            return sup(name)

    def contribute_to_class(self):
        if isinstance(self.to, six.string_types) or self.to._meta.pk is None:
            def resolve_related_class(rel, model, cls):
                rel.to = model
                rel.do_related_class()
            add_lazy_relation(self.field.model, self, self.to,
                              resolve_related_class)
        else:
            self.do_related_class()

    def do_related_class(self):
        # check that the relation does not already exist
        all_rels = self.field.rel.rels
        if self.to in [r.to for r in all_rels if r != self]:
            # if it does, it needs to be removed from the list, and no further
            # action should be taken
            all_rels.remove(self)
            return

        self.related = GM2MRelation(self.field.model, self.field, self)
        if not self.field.model._meta.abstract:
            self.contribute_to_related_class()

    def contribute_to_related_class(self):
        """
        Appends accessors to related classes
        """

        # this enables cascade deletion for any relation (even hidden ones)
        add_related_field(self.to._meta, self.related)

        if self.on_delete in handlers_with_signal:
            # if a signal should be sent on deletion, we connect a dummy
            # receiver to pre_delete so that the model is not
            # 'fast_delete'-able
            # (see django.db.models.deletion.Collector.can_fast_delete)
            pre_delete.connect(self.dummy_pre_delete, sender=self.to)

        # Internal M2Ms (i.e., those with a related name ending with '+')
        # and swapped models don't get a related descriptor.
        if not self.is_hidden() and not is_swapped(self.field.model):
            setattr(self.to, self.related_name
                        or (get_model_name(self.field.model._meta) + '_set'),
                    GM2MRelatedDescriptor(self.related, self))

    @cached_property
    def related_manager_cls(self):
        # the related manager class getter is implemented here rather than in
        # the descriptor as we may need to access it even for hidden relations
        return create_gm2m_related_manager(
            superclass=self.to._default_manager.__class__,
            field=self.related.field,
            model=self.field.model,
            through=self.through,
            query_field_name=get_model_name(self.through),
            field_names=self.through._meta._field_names,
            prefetch_cache_name=self.related.field.related_query_name()
        )

    @property
    def swappable_setting(self):
        """
        Gets the setting that this is powered from for swapping, or None
        if it's not swapped in
        """
        # can only be called by Django 1.7+, the apps module will be available

        # Work out string form of "to"
        if isinstance(self.to, six.string_types):
            to_string = self.to
        else:
            to_string = "%s.%s" % (
                self.to._meta.app_label,
                self.to._meta.object_name,
            )
        # See if anything swapped/swappable matches
        for model in apps.get_models(include_swapped=True):
            if model._meta.swapped == to_string \
            or model._meta.swappable \
            and ("%s.%s" % (model._meta.app_label,
                            model._meta.object_name)) == to_string:
                return model._meta.swappable
        return None


class GM2MRel(object):

    to = ''  # faking a 'normal' relation for Django 1.7

    def __init__(self, field, related_models, **params):

        self.field = field

        for name, default in six.iteritems(REL_ATTRS):
            setattr(self, name, params.pop(name, default))
        for name, value in six.iteritems(REL_ATTRS_FIXED):
            setattr(self, name, value)

        self.on_delete_src = params.pop('on_delete_src', self.on_delete)
        self.on_delete_tgt = params.pop('on_delete_tgt', self.on_delete)

        if self.through and not self.db_constraint:
            raise ValueError('django-gm2m: Can\'t supply a through model '
                             'with db_constraint=False')

        self.rels = []
        for model in related_models:
            self.add_relation(model, contribute_to_class=False)

    def add_relation(self, model, contribute_to_class=True):
        try:
            assert not model._meta.abstract, \
            "%s cannot define a relation with abstract class %s" \
            % (self.field.__class__.__name__, model._meta.object_name)
        except AttributeError:
            # to._meta doesn't exist, so it must be a string
            assert isinstance(model, six.string_types), \
            '%s(%r) is invalid. First parameter to GM2MField must ' \
            'be either a model or a model name' \
            % (self.field.__class__.__name__, model)

        rel = GM2MUnitRel(self.field, model)
        self.rels.append(rel)
        if contribute_to_class:
            rel.contribute_to_class()
        return rel

    def check(self, **kwargs):
        errors = []
        for rel in self.rels:
            errors.extend(rel.check(**kwargs))
        errors.extend(self._check_relationship_model(**kwargs))
        return errors

    def _check_relationship_model(self, from_model=None, **kwargs):
        if hasattr(self.through, '_meta'):
            qualified_model_name = "%s.%s" % (
                self.through._meta.app_label, self.through.__name__)
        else:
            qualified_model_name = self.through

        errors = []

        if self.through not in apps.get_models(include_auto_created=True):
            # The relationship model is not installed.
            errors.append(
                checks.Error(
                    ("Field specifies a many-to-many relation through model "
                     "'%s', which has not been installed.") %
                    qualified_model_name,
                    hint=None,
                    obj=self,
                    id='gm2m.E101',
                )
            )

        else:

            assert from_model is not None, \
                "GM2MField with intermediate " \
                "tables cannot be checked if you don't pass the model " \
                "where the field is attached to."

            # Set some useful local variables
            from_model_name = from_model._meta.object_name

            # Count foreign keys in intermediate model
            seen_from = sum(from_model == getattr(field.rel, 'to', None)
                for field in self.through._meta.fields)

            if seen_from == 0:
                errors.append(
                    checks.Error(
                        ("The model is used as an intermediate model by '%s', "
                         "but it does not have a foreign key to '%s' or a "
                         "generic foreign key.") % (self, from_model_name),
                        hint=None,
                        obj=self.through,
                        id='gm2m.E102',
                    )
                )
            elif seen_from > 1 and not self.through_fields:
                errors.append(
                    checks.Warning(
                        "The model is used as an intermediate model by "
                        "'%s', but it has more than one foreign key "
                        "from '%s', which is ambiguous. You must specify "
                        "which foreign key Django should use via the "
                        "through_fields keyword argument."
                        % (self, from_model_name),
                        hint=None,
                        obj=self,
                        id='gm2m.E103',
                    )
                )

            seen_to = sum(isinstance(field, GenericForeignKey)
                for field in self.through._meta.virtual_fields)

            if seen_to == 0:
                errors.append(
                    checks.Error(
                        "The model is used as an intermediate model by "
                         "'%s', but it does not have a a generic foreign key."
                         % (self, from_model_name),
                        hint=None,
                        obj=self.through,
                        id='gm2m.E104',
                    )
                )
            elif seen_to > 1 and not self.through_fields:
                errors.append(
                    checks.Warning(
                        "The model is used as an intermediate model by "
                        "'%s', but it has more than one generic foreign "
                        "key, which is ambiguous. You must specify "
                        "which generic foreign key Django should use via "
                        "the through_fields keyword argument." % self,
                        hint=None,
                        obj=self,
                        id='gm2m.E105',
                    )
                )

        # Validate `through_fields`
        if self.through_fields is not None:
            # Validate that we're given an iterable of at least two items
            # and that none of them is "falsy"
            if not (len(self.through_fields) >= 2 and
                    self.through_fields[0] and self.through_fields[1]):
                errors.append(
                    checks.Error(
                        ("Field specifies 'through_fields' but does not "
                         "provide the names of the two link fields that "
                         "should be used for the relation through model "
                         "'%s'.") % qualified_model_name,
                        hint=("Make sure you specify 'through_fields' as "
                              "through_fields=('field1', 'field2')"),
                        obj=self,
                        id='gm2m.E106',
                    )
                )

            # Validate the given through fields -- they should be actual
            # fields on the through model, and also be foreign keys to the
            # expected models
            else:
                assert from_model is not None, \
                    "GM2MField with intermediate " \
                    "tables cannot be checked if you don't pass the model " \
                    "where the field is attached to."

                src_field_name = self.through_fields[0]
                through = self.through

                possible_field_names = []
                for f in through._meta.fields:
                    if hasattr(f, 'rel') \
                    and getattr(f.rel, 'to', None) == from_model:
                        possible_field_names.append(f.name)
                if possible_field_names:
                    hint = ("Did you mean one of the following foreign keys "
                            "to '%s': %s?") % (from_model._meta.object_name,
                                               ', '.join(possible_field_names))
                else:
                    hint = None

                try:
                    field = through._meta.get_field(src_field_name)
                except FieldDoesNotExist:
                    errors.append(
                        checks.Error(
                            "The intermediary model '%s' has no field '%s'."
                            % (qualified_model_name, src_field_name),
                            hint=hint,
                            obj=self,
                            id='gm2m.E107',
                        )
                    )
                else:
                    if not (hasattr(field, 'rel') and
                            getattr(field.rel, 'to', None) == from_model):
                        errors.append(
                            checks.Error(
                                "'%s.%s' is not a foreign key to '%s'." % (
                                    through._meta.object_name, src_field_name,
                                    from_model._meta.object_name),
                                hint=hint,
                                obj=self,
                                id='gm2m.E108',
                            )
                        )

                target_field_name = self.through_fields[1]

                possible_field_names = []
                for f in through._meta.virtual_fields:
                    if isinstance(f, GenericForeignKey):
                        possible_field_names.append(f.name)
                if possible_field_names:
                    hint = "Did you mean one of the following generic " \
                           "foreign keys: %s?" \
                           % ', '.join(possible_field_names)
                else:
                    hint = None

                field = None
                for f in through._meta.virtual_fields:
                    if f.name == target_field_name:
                        field = f
                        break
                else:
                    errors.append(
                        checks.Error(
                            "The intermediary model '%s' has no generic "
                            "foreign key named '%s'."
                            % (qualified_model_name, src_field_name),
                            hint=hint,
                            obj=self,
                            id='gm2m.E109',
                        )
                    )

                if field:
                    if not isinstance(field, GenericForeignKey):
                        errors.append(
                            checks.Error(
                                "'%s.%s' is not a generic foreign key."
                                % (through._meta.object_name, src_field_name),
                                hint=hint,
                                obj=self,
                                id='gm2m.E110',
                            )
                        )
        return errors

    def contribute_to_class(self, cls, virtual_only=False):

        # Connect the descriptor for this field
        setattr(cls, self.field.attname,
                ReverseGM2MRelatedDescriptor(self.field))

        if not self.through:
            self.through = create_gm2m_intermediary_model(self.field, cls)
            self.through_fields = None

        # set related name
        if not self.field.model._meta.abstract and self.related_name:
            self.related_name = self.related_name % {
                'class': self.field.model.__name__.lower(),
                'app_label': self.field.model._meta.app_label.lower()
            }

        def calc_field_names(rel):
            # Extract field names from through model
            field_names = {}

            if rel.through_fields:
                field_names['src'], field_names['tgt'] = \
                    rel.through_fields[:2]
                for gfk in rel.through._meta.virtual_fields:
                    if gfk.name == field_names['tgt']:
                        break
                else:
                    raise FieldDoesNotExist(
                        'Generic foreign key "%s" does not exist in through '
                        'model "%s"' % (field_names['tgt'],
                                        get_model_name(rel.through))
                    )
                field_names['tgt_ct'] = gfk.ct_field
                field_names['tgt_fk'] = gfk.fk_field
            else:
                for f in rel.through._meta.fields:
                    if hasattr(f, 'rel') and f.rel \
                    and (f.rel.to == rel.field.model
                         or f.rel.to == '%s.%s' % (
                            rel.field.model._meta.app_label,
                            rel.field.model._meta.object_name)):
                        field_names['src'] = f.name
                        break
                for f in rel.through._meta.virtual_fields:
                    if isinstance(f, GenericForeignKey):
                        field_names['tgt'] = f.name
                        field_names['tgt_ct'] = f.ct_field
                        field_names['tgt_fk'] = f.fk_field
                        break

            if not is_fake_model(rel.through) \
            and not set(field_names.keys()).issuperset(('src', 'tgt')):
                raise ValueError('Bad through model for GM2M relationship.')

            rel.through._meta._field_names = field_names

        # resolve through model if it's provided as a string
        if isinstance(self.through, six.string_types):
            def resolve_through_model(rel, model, cls):
                self.through = model
                calc_field_names(rel)
            add_lazy_relation(cls, self, self.through, resolve_through_model)
        else:
            calc_field_names(self)

        for rel in self.rels:
            rel.contribute_to_class()

    @cached_property
    def related_manager_cls(self):
        field_names = self.through._meta._field_names
        return create_gm2m_related_manager(
            superclass=None,
            field=self.field,
            model=self.through,
            through=self.through,
            query_field_name=field_names['src'],
            field_names=field_names,
            prefetch_cache_name=self.field.name
        )
