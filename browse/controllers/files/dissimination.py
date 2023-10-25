"""Controller for PDF, source and other downloads."""

import logging
from email.utils import format_datetime
from functools import partial
from typing import Callable, Optional, Union

from browse.domain.identifier import Identifier, IdentifierException
from browse.domain.fileformat import FileFormat
from browse.domain.version import VersionEntry
from browse.domain.metadata import DocMetadata
from browse.domain import fileformat

from browse.controllers.files import stream_gen, last_modified, add_time_headers

from browse.services.object_store.fileobj import FileObj, UngzippedFileObj, FileFromTar, FileDoesNotExist
from browse.services.object_store.object_store_gs import GsObjectStore
from browse.services.object_store.object_store_local import LocalObjectStore

from browse.services.documents import get_doc_service
from browse.services.html_processing import post_process_html2

from browse.services.dissemination import get_article_store
from browse.services.dissemination.article_store import (
    Acceptable_Format_Requests, CannotBuildPdf, Deleted)
from browse.services.next_published import next_publish

from browse.stream.file_processing import process_file
from browse.stream.tarstream import tar_stream_gen
from flask import Response, abort, make_response, render_template, current_app
from flask_rangerequest import RangeRequest
from werkzeug.exceptions import BadRequest
from google.cloud import storage


logger = logging.getLogger(__file__)
logger.setLevel(logging.INFO)


Resp_Fn_Sig = Callable[[FileFormat, FileObj, Identifier, DocMetadata,
                        VersionEntry], Response]


def default_resp_fn(format: FileFormat,
                    file: FileObj,
                    arxiv_id: Identifier,
                    docmeta: DocMetadata,
                    version: VersionEntry,
                    extra: Optional[str] = None) -> Response:
    """Creates a response with approprate headers for the `file`.

    Parameters
    ----------
    format : FileFormat
        `FileFormat` of the `file`
    item : DocMetadata
        article that the response is for.
    file : FileObj
        File object to use in the response.
    """

    # Have to do Range Requests to get GCP CDN to accept larger objects.
    resp: Response = RangeRequest(file.open('rb'),
                                  etag=last_modified(file),
                                  last_modified=file.updated,
                                  size=file.size).make_response()

    resp.headers['Access-Control-Allow-Origin'] = '*'
    if isinstance(format, FileFormat):
        resp.headers['Content-Type'] = format.content_type

    if resp.status_code == 200:
        # For large files on CloudRun chunked and no content-length needed
        # TODO revisit this, in some cases it doesn't work maybe when
        # combined with gzip encoding?
        # resp.headers['Transfer-Encoding'] = 'chunked'
        resp.headers.pop('Content-Length')

    add_time_headers(resp, file, arxiv_id)
    return resp


def src_resp_fn(format: FileFormat,
                file: FileObj,
                arxiv_id: Identifier,
                docmeta: DocMetadata,
                version: VersionEntry,
                extra: Optional[str] = None) -> Response:
    """Prepares a response where the payload will be a tar of the source.

    No matter what the actual format of the source, this will try to return a
    .tar.  If the source is a .pdf then that will be tarred. If the source is a
    gzipped PS file, that will be ungzipped and then tarred.

    This will also uses gzipped transfer encoding. But the client will unencode
    the bytestream and the file will be saved as .tar.
    """
    if file.name.endswith(".tar.gz"):  # Nothing extra to do, already .tar.gz
        resp = RangeRequest(file.open('rb'), etag=last_modified(file),
                            last_modified=file.updated,
                            size=file.size).make_response()
    elif file.name.endswith(".gz"):  # unzip single file gz and then tar
        outstream = tar_stream_gen([UngzippedFileObj(file)])
        resp = make_response(outstream, 200)
    else:  # tar single flie like .pdf
        outstream = tar_stream_gen([file])
        resp = make_response(outstream, 200)

    archive = f"{arxiv_id.archive}-" if arxiv_id.is_old_id else ""
    filename = f"arXiv-{archive}{arxiv_id.filename}v{version.version}.tar"

    resp.headers["Content-Encoding"] = "x-gzip"  # tar_stream_gen() gzips
    resp.headers["Content-Type"] = "application/x-eprint-tar"
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=\"{filename}\""
    add_time_headers(resp, file, arxiv_id)
    resp.headers["ETag"] = last_modified(file)
    return resp  # type: ignore


def get_src_resp(arxiv_id_str: str,
                 archive: Optional[str] = None) -> Response:
    return get_dissimination_resp("e-print", arxiv_id_str, archive,
                                  src_resp_fn)


def get_e_print_resp(arxiv_id_str: str,
                     archive: Optional[str] = None) -> Response:
    return get_dissimination_resp("e-print", arxiv_id_str, archive)


def get_dissimination_resp(format: Acceptable_Format_Requests,
                           arxiv_id_str: str,
                           archive: Optional[str] = None,
                           resp_fn: Resp_Fn_Sig = default_resp_fn) -> Response:
    """
    Returns a `Flask` response ojbject for a given `arxiv_id` and `FileFormat`.

    The response will include headers and may do a range response.
    """
    arxiv_id_str = f"{archive}/{arxiv_id_str}" if archive else arxiv_id_str
    try:
        if len(arxiv_id_str) > 40:
            abort(400)
        if arxiv_id_str.startswith('arxiv/'):
            abort(400, description="do not prefix non-legacy ids with arxiv/")
        arxiv_id = Identifier(arxiv_id_str)
    except IdentifierException as ex:
        return bad_id(arxiv_id_str, str(ex))

    item = get_article_store().dissemination(format, arxiv_id)
    logger. debug(f"dissemination_for_id({arxiv_id.idv}) was {item}")
    if not item or item == "VERSION_NOT_FOUND" or item == "ARTICLE_NOT_FOUND":
        return not_found(arxiv_id)
    elif item == "WITHDRAWN" or item == "NO_SOURCE":
        return withdrawn(arxiv_id)
    elif item == "UNAVAIABLE":
        return unavailable(arxiv_id)
    elif item == "NOT_PDF":
        return not_pdf(arxiv_id)
    elif isinstance(item, Deleted):
        return bad_id(arxiv_id, item.msg)
    elif isinstance(item, CannotBuildPdf):
        return cannot_build_pdf(arxiv_id, item.msg)

    file, item_format, docmeta, version = item
    if not file.exists():
        return not_found(arxiv_id)

    return resp_fn(item_format, file, arxiv_id, docmeta, version)

def _get_latexml_conversion_file (arxiv_id: Identifier) -> Union[str, FileObj]: # str here should be the conditions
    obj_store = GsObjectStore(storage.Client().bucket(current_app.config['LATEXML_BUCKET']))
    if arxiv_id.extra:
        item = obj_store.to_obj(f'{arxiv_id.idv}/{arxiv_id.extra}')
        if isinstance(item, FileDoesNotExist):
            return "NO_SOURCE" # TODO: This could be more specific
    else:
        item = obj_store.to_obj(f'{arxiv_id.idv}/{arxiv_id.idv}.html')
        if isinstance(item, FileDoesNotExist):
            return "ARTICLE_NOT_FOUND"

def _html_response(file: FileObj,
                arxiv_id: Identifier,
                docmeta: DocMetadata,
                version: VersionEntry,
                extra: Optional[str] = None):
    resp = make_response(file, 200)
    add_time_headers(resp, file, arxiv_id)
    resp.headers["Content-Type"] = "text/html"
    resp.headers["ETag"] = last_modified(file)
    return resp  # type: ignore


def _guess_response(file, arxiv_id, docmeta, version, extra):
    # resp = make_response(file, 200)
    # based on file.name guess a Content-Type
    # make headers time headers, maybe etag
    pass


def html_source_response_function(format: FileFormat,
                file: FileObj,
                arxiv_id: Identifier,
                docmeta: DocMetadata,
                version: VersionEntry,
                extra: Optional[str] = None):
    # Not needed since done in get_dissimination_resp
    # if not file.exists():
    #     return not_found(arxiv_id.ids)
    if file.name.endswith(".html.gz") and extra:
        # todo need return a 404 for this path
        return not_found_anc() # todo something like new fn not_found_html()

    extra = extra or "index.html"
    unzipped_file = UngzippedFileObj(file)

    if unzipped_file.name.endswith(".html"):  # handle single html files here
        requested_file = unzipped_file
    else:
        requested_file = FileFromTar(unzipped_file, path=extra)

    if requested_file.name.endswith(".html"):
        # TODO use example class sent via slack
        # processed_file = TransformFileObj(requested_file, preprocess_html)
        return _html_response(processed_file, arxiv_id, docmeta, version, extra)
    else:
        return _guess_response(requested_file, arxiv_id, docmeta, version, extra)

def html_latexml_response_function(format: FileFormat,
                file: FileObj,
                arxiv_id: Identifier,
                docmeta: DocMetadata,
                version: VersionEntry,
                extra: Optional[str] = None):
    # get latexml file obj
    # file = something_or_other(...)
    # return = _html_response(file, arxiv_id, docmeta, version, extra)
    pass


def html_response_function(format: FileFormat,
                file: FileObj,
                arxiv_id: Identifier,
                docmeta: DocMetadata,
                version: VersionEntry,
                extra: Optional[str] = None) -> Response:
    if docmeta.source_format == 'html': #TODO find a way to distinguish that works. perhaps all the latex files have a latex source?
        return html_source_response_function(format,file, arxiv_id, docmeta, version, extra)
    else:
        return html_latexml_response_function(format,file, arxiv_id, docmeta, version, extra)


def get_html_response_example(arxiv_id_str: str,
                              archive: Optional[str] = None,
                              extra: Optional[str] = None) -> Response:
    """Handles both html source and latexml responses."""
    resp_fn = partial( html_response_function, extra=extra)
    # We'd probably like to reuse get_discrimination_resp and if it is too PDF specific we should fix that.
    return get_dissimination_resp(fileformat.html_source, arxiv_id_str, archive, resp_fn=resp_fn)


def get_html_response(arxiv_id_str: str,
                           archive: Optional[str] = None,
                           resp_fn: Resp_Fn_Sig = default_resp_fn) -> Response:
    # if arxiv_id_str.endswith('.html'):
    #     return redirect(f'/html/{arxiv_id.split(".html")[0]}') 
    #TODO possibly add handling for .html at end of path, doesnt currently work on legacy either, currently causes Identifier Exception

    arxiv_id_str = f"{archive}/{arxiv_id_str}" if archive else arxiv_id_str
    try:
        if len(arxiv_id_str) > 40:
            abort(400)
        if arxiv_id_str.startswith('arxiv/'):
            abort(400, description="do not prefix non-legacy ids with arxiv/")
        arxiv_id = Identifier(arxiv_id_str)
    except IdentifierException as ex:
        return bad_id(arxiv_id_str, str(ex))

    metadata = get_doc_service().get_abs(arxiv_id)

    if metadata.source_format == 'html': #TODO find a way to distinguish that works. perhaps all the latex files have a latex source?
        native_html = True
        #TODO doesnt brian C have some sort of abstraction so we dont have to do this
        if not current_app.config["DISSEMINATION_STORAGE_PREFIX"].startswith("gs://"):
            obj_store = LocalObjectStore(current_app.config["DISSEMINATION_STORAGE_PREFIX"])
            #TODO would these files also come gzipped or some other format
        else:
            obj_store = GsObjectStore(storage.Client().bucket(
                current_app.config["DISSEMINATION_STORAGE_PREFIX"].replace('gs://', '')))
            
        item = get_article_store().dissemination(fileformat.html_source, arxiv_id)

       
    else:
        native_html = False
        #TODO assign some of the other variables like item format
        #you probably want to wire this one to go through dissemination too
        requested_file = _get_latexml_conversion_file(arxiv_id)
        if not arxiv_id.extra:
            return requested_file # Serve static asset
        docmeta = metadata
        version = docmeta.version
        item_format = fileformat.html_source

    if not item or item == "VERSION_NOT_FOUND" or item == "ARTICLE_NOT_FOUND":
            return not_found(arxiv_id)
    elif item == "WITHDRAWN" or item == "NO_SOURCE":
        return withdrawn(arxiv_id)
    elif item == "UNAVAIABLE":
        return unavailable(arxiv_id)
    elif isinstance(item, Deleted):
        return bad_id(arxiv_id, item.msg)

    if native_html:
        gzipped_file, item_format, docmeta, version = item
        if not gzipped_file.exists():
            return not_found(arxiv_id)
        #TODO some sort of error handlingfor not beign able to retrieve file, draft in conference proceeding.py
        unzipped_file=UngzippedFileObj(gzipped_file)

        if unzipped_file.name.endswith(".html"): #handle single html files here
            requested_file=unzipped_file
        else:
            tar=unzipped_file
        #TODO process file here
        with requested_file.open('rb') as f:
            output= process_file(f,post_process_html2) #TODO put this into a file object

    response=default_resp_fn(item_format,requested_file,arxiv_id,docmeta,version)
    if native_html: 
        """special cases for the fact that documents within conference proceedings can change 
        which will appear differently in the conference proceeding even if the conference proceeding paper stays the same"""
        response.headers['Expires'] = format_datetime(next_publish())
        #TODO handle file modification times here

    return response

def withdrawn(arxiv_id: str) -> Response:
    """Sets expire to one year, max allowed by RFC 2616"""
    headers = {'Cache-Control': 'max-age=31536000'}
    return make_response(render_template("pdf/withdrawn.html",
                                         arxiv_id=arxiv_id),
                         200, headers)


def unavailable(arxiv_id: str) -> Response:
    return make_response(render_template("pdf/unavaiable.html",
                                         arxiv_id=arxiv_id), 500, {})


def not_pdf(arxiv_id: str) -> Response:
    return make_response(render_template("pdf/unavaiable.html",
                                         arxiv_id=arxiv_id), 404, {})


def not_found(arxiv_id: str) -> Response:
    headers = {'Expires': format_datetime(next_publish())}
    return make_response(render_template("pdf/not_found.html",
                                         arxiv_id=arxiv_id), 404, headers)


def not_found_anc(arxiv_id: str) -> Response:
    headers = {'Expires': format_datetime(next_publish())}
    return make_response(render_template("src/anc_not_found.html",
                                         arxiv_id=arxiv_id), 404, headers)


def bad_id(arxiv_id: str, err_msg: str) -> Response:
    return make_response(render_template("pdf/bad_id.html",
                                         err_msg=err_msg,
                                         arxiv_id=arxiv_id), 404, {})


def cannot_build_pdf(arxiv_id: str, msg: str) -> Response:
    return make_response(render_template("pdf/cannot_build_pdf.html",
                                         err_msg=msg,
                                         arxiv_id=arxiv_id), 404, {})
