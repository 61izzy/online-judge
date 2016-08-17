from textwrap import dedent

from django import forms
from django.conf import settings
from django.contrib.admin import widgets as admin_widgets
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import FieldError
from django.forms.utils import flatatt
from django.template import Context, Template
from django.template.loader import get_template
from django.utils.encoding import force_unicode
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe
from lxml import html


class CheckboxSelectMultipleWithSelectAll(forms.CheckboxSelectMultiple):
    _all_selected = False

    def render(self, *args, **kwargs):
        empty = False
        if not self.choices:
            empty = True
        has_id = kwargs and ('attrs' in kwargs) and ('id' in kwargs['attrs'])
        if not has_id:
            raise FieldError('id required')
        select_all_id = kwargs['attrs']['id'] + '_all'
        select_all_name = args[0] + '_all'
        renderer = super(CheckboxSelectMultipleWithSelectAll, self).get_renderer(*args, **kwargs)
        template = get_template('widgets/select_all.jade')
        context = Context({'original_widget': renderer.render(),
                           'select_all_id': select_all_id,
                           'select_all_name': select_all_name,
                           'all_selected': all(choice[0] in renderer.value for choice in renderer.choices),
                           'empty': empty})
        return mark_safe(template.render(context))

    def value_from_datadict(self, *args, **kwargs):
        original = super(CheckboxSelectMultipleWithSelectAll, self).value_from_datadict(*args, **kwargs)
        select_all_name = args[2] + '_all'
        if select_all_name in args[0]:
            self._all_selected = True
        else:
            self._all_selected = False
        return original


class CompressorWidgetMixin(object):
    __template_css = dedent('''\
        {% compress css %}
            {{ media.css }}
        {% endcompress %}
    ''')

    __template_js = dedent('''\
        {% compress js %}
            {{ media.js }}
        {% endcompress %}
    ''')

    __templates = {
        (False, False): Template(''),
        (True, False): Template('{% load compress %}' + __template_css),
        (False, True): Template('{% load compress %}' + __template_js),
        (True, True): Template('{% load compress %}' + __template_js + __template_css),
    }

    compress_css = False
    compress_js = False

    try:
        import compressor
    except ImportError:
        pass
    else:
        if getattr(settings, 'COMPRESS_ENABLED', not getattr(settings, 'DEBUG', False)):
            def _media(self):
                media = super(CompressorWidgetMixin, self)._media()
                template = self.__templates[self.compress_css, self.compress_js]
                result = html.fromstring(template.render({'media': media}))
                return forms.Media(
                        css={'all': [result.find('.//link').get('href')]} if self.compress_css else media._css,
                        js=[result.find('.//script').get('src')] if self.compress_js else media._js
                )
        
            media = property(_media)


try:
    from pagedown.widgets import PagedownWidget as OldPagedownWidget
except ImportError:
    PagedownWidget = None
    AdminPagedownWidget = None
    MathJaxPagedownWidget = None
    MathJaxAdminPagedownWidget = None
else:
    class PagedownWidget(CompressorWidgetMixin, OldPagedownWidget):
        compress_js = True

        def __init__(self, *args, **kwargs):
            kwargs.setdefault('css', (staticfiles_storage.url('pagedown_widget.css'),))
            super(PagedownWidget, self).__init__(*args, **kwargs)


    class AdminPagedownWidget(PagedownWidget, admin_widgets.AdminTextareaWidget):
        def _media(self):
            media = super(AdminPagedownWidget, self)._media()
            media.add_css({'all': [
                staticfiles_storage.url('content-description.css'),
                staticfiles_storage.url('admin/css/pagedown.css'),
            ]})
            media.add_js([staticfiles_storage.url('admin/js/pagedown.js')])
            return media

        media = property(_media)


    class MathJaxPagedownWidget(PagedownWidget):
        def _media(self):
            media = super(MathJaxPagedownWidget, self)._media()
            media.add_js([
                staticfiles_storage.url('mathjax_config.js'),
                '//cdn.mathjax.org/mathjax/latest/MathJax.js?config=TeX-AMS-MML_HTMLorMML',
                staticfiles_storage.url('pagedown_math.js'),
            ])
            return media

        media = property(_media)


    class MathJaxAdminPagedownWidget(AdminPagedownWidget, MathJaxPagedownWidget):
        pass


    class HeavyPreviewPageDownWidget(PagedownWidget):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('template', 'pagedown.jade')
            self.preview_url = kwargs.pop('preview')
            super(HeavyPreviewPageDownWidget, self).__init__(*args, **kwargs)

        def render(self, name, value, attrs=None):
            if value is None:
                value = ''
            final_attrs = self.build_attrs(attrs, name=name)
            if 'class' not in final_attrs:
                final_attrs['class'] = ''
            final_attrs['class'] += ' wmd-input'
            return get_template(self.template).render(self.get_template_context(final_attrs, value))

        def get_template_context(self, attrs, value):
            return {
                'attrs': flatatt(attrs),
                'body': conditional_escape(force_unicode(value)),
                'id': attrs['id'],
                'show_preview': self.show_preview,
                'preview_url': self.preview_url
            }

        def _media(self):
            media = super(HeavyPreviewPageDownWidget, self)._media()
            media.add_css({'all': [staticfiles_storage.url('dmmd-preview.css'),]})
            media.add_js([staticfiles_storage.url('dmmd-preview.js')])
            return media

        media = property(_media)

    class HeavyPreviewAdminPageDownWidget(AdminPagedownWidget, HeavyPreviewPageDownWidget):
        def _media(self):
            media = super(AdminPagedownWidget, self)._media()
            media.add_css({'all': [
                staticfiles_storage.url('pygment-github.css'),
            ]})
            return media

        media = property(_media)
