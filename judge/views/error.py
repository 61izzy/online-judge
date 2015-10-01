import traceback

from django.shortcuts import *


def error(request, context, status):
    template = loader.get_template('error.jade')
    return HttpResponse(template.render(RequestContext(request, context)), status=status)


def error404(request):
    # TODO: "panic: go back"
    template = loader.get_template('generic_message.jade')
    return HttpResponse(template.render(RequestContext(request, {
        'title': '404 error',
        'message': 'Could not find page "%s"' % request.path})), status=404)


def error403(request):
    return error(request, {'id': 'unauthorized_access',
                           'description': 'no permission for %s' % request.path,
                           'code': 403}, 403)


def error500(request):
    return error(request, {'id': 'invalid_state',
                           'description': 'corrupt page %s' % request.path,
                           'traceback': traceback.format_exc(),
                           'code': 500}, 500)
