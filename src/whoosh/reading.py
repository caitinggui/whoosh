# Copyright 2007 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.

"""This module contains classes that allow reading from an index.
"""

from math import log
from bisect import bisect_left, bisect_right
from heapq import heapify, heapreplace, heappop, nlargest

from whoosh import columns, fst
from whoosh.compat import abstractmethod
from whoosh.compat import xrange, zip_, next, iteritems
from whoosh.filedb.filestore import OverlayStorage
from whoosh.matching import MultiMatcher
from whoosh.support.levenshtein import distance
from whoosh.system import emptybytes


# Exceptions

class TermNotFound(Exception):
    pass


class NoGraphError(Exception):
    pass


# Term Info base class

class TermInfo(object):
    """Represents a set of statistics about a term. This object is returned by
    :meth:`IndexReader.term_info`. These statistics may be useful for
    optimizations and scoring algorithms.
    """

    def __init__(self, weight=0, df=0, minlength=None,
                 maxlength=0, maxweight=0, minid=None, maxid=0):
        self._weight = weight
        self._df = df
        self._minlength = minlength
        self._maxlength = maxlength
        self._maxweight = maxweight
        self._minid = minid
        self._maxid = maxid

    def add_posting(self, docnum, weight, length=None):
        if self._minid is None:
            self._minid = docnum
        self._maxid = docnum
        self._weight += weight
        self._df += 1
        self._maxweight = max(self._maxweight, weight)

        if length is not None:
            if self._minlength is None:
                self._minlength = length
            else:
                self._minlength = min(self._minlength, length)
            self._maxlength = max(self._maxlength, length)

    def weight(self):
        """Returns the total frequency of the term across all documents.
        """

        return self._weight

    def doc_frequency(self):
        """Returns the number of documents the term appears in.
        """

        return self._df

    def min_length(self):
        """Returns the length of the shortest field value the term appears
        in.
        """

        return self._minlength

    def max_length(self):
        """Returns the length of the longest field value the term appears
        in.
        """

        return self._maxlength

    def max_weight(self):
        """Returns the number of times the term appears in the document in
        which it appears the most.
        """

        return self._maxweight

    def min_id(self):
        """Returns the lowest document ID this term appears in.
        """

        return self._minid

    def max_id(self):
        """Returns the highest document ID this term appears in.
        """

        return self._maxid


# Reader base class

class IndexReader(object):
    """Do not instantiate this object directly. Instead use Index.reader().
    """

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @abstractmethod
    def __contains__(self, term):
        """Returns True if the given term tuple (fieldname, text) is
        in this reader.
        """
        raise NotImplementedError

    def is_atomic(self):
        return True

    def _text_to_bytes(self, fieldname, text):
        if fieldname not in self.schema:
            raise TermNotFound((fieldname, text))
        return self.schema[fieldname].to_bytes(text)

    def close(self):
        """Closes the open files associated with this reader.
        """

        pass

    def generation(self):
        """Returns the generation of the index being read, or -1 if the backend
        is not versioned.
        """

        return None

    @abstractmethod
    def all_terms(self):
        """Yields (fieldname, text) tuples for every term in the index.
        """

        raise NotImplementedError

    def terms_from(self, fieldname, prefix):
        """Yields (fieldname, text) tuples for every term in the index starting
        at the given prefix.
        """

        # The default implementation just scans the whole list of terms
        for fname, text in self.all_terms():
            if fname < fieldname or text < prefix:
                continue
            yield (fname, text)

    @abstractmethod
    def term_info(self, fieldname, text):
        """Returns a :class:`TermInfo` object allowing access to various
        statistics about the given term.
        """

        raise NotImplementedError

    def expand_prefix(self, fieldname, prefix):
        """Yields terms in the given field that start with the given prefix.
        """

        for fn, text in self.terms_from(fieldname, prefix):
            if fn != fieldname or not text.startswith(prefix):
                return
            yield text

    def lexicon(self, fieldname):
        """Yields all bytestrings in the given field.
        """

        for fn, btext in self.terms_from(fieldname, emptybytes):
            if fn != fieldname:
                return
            yield btext

    def field_terms(self, fieldname):
        """Yields all term values (converted from on-disk bytes) in the given
        field.
        """

        from_bytes = self.schema[fieldname].from_bytes
        for btext in self.lexicon(fieldname):
            yield from_bytes(btext)

    def __iter__(self):
        """Yields ((fieldname, text), terminfo) tuples for each term in the
        reader, in lexical order.
        """

        term_info = self.term_info
        for term in self.all_terms():
            yield (term, term_info(*term))

    def iter_from(self, fieldname, text):
        """Yields ((fieldname, text), terminfo) tuples for all terms in the
        reader, starting at the given term.
        """

        term_info = self.term_info
        text = self._text_to_bytes(fieldname, text)
        for term in self.terms_from(fieldname, text):
            yield (term, term_info(*term))

    def iter_field(self, fieldname, prefix=''):
        """Yields (text, terminfo) tuples for all terms in the given field.
        """

        prefix = self._text_to_bytes(fieldname, prefix)
        for (fn, text), terminfo in self.iter_from(fieldname, prefix):
            if fn != fieldname:
                return
            yield text, terminfo

    def iter_prefix(self, fieldname, prefix):
        """Yields (text, terminfo) tuples for all terms in the given field with
        a certain prefix.
        """

        prefix = self._text_to_bytes(fieldname, prefix)
        for text, terminfo in self.iter_field(fieldname, prefix):
            if not text.startswith(prefix):
                return
            yield (text, terminfo)

    @abstractmethod
    def has_deletions(self):
        """Returns True if the underlying index/segment has deleted
        documents.
        """

        raise NotImplementedError

    def all_doc_ids(self):
        """Returns an iterator of all (undeleted) document IDs in the reader.
        """

        is_deleted = self.is_deleted
        return (docnum for docnum in xrange(self.doc_count_all())
                if not is_deleted(docnum))

    def iter_docs(self):
        """Yields a series of ``(docnum, stored_fields_dict)``
        tuples for the undeleted documents in the reader.
        """

        for docnum in self.all_doc_ids():
            yield docnum, self.stored_fields(docnum)

    @abstractmethod
    def is_deleted(self, docnum):
        """Returns True if the given document number is marked deleted.
        """

        raise NotImplementedError

    @abstractmethod
    def stored_fields(self, docnum):
        """Returns the stored fields for the given document number.

        :param numerickeys: use field numbers as the dictionary keys instead of
            field names.
        """

        raise NotImplementedError

    def all_stored_fields(self):
        """Yields the stored fields for all documents (including deleted
        documents).
        """

        for docnum in xrange(self.doc_count_all()):
            yield self.stored_fields(docnum)

    @abstractmethod
    def doc_count_all(self):
        """Returns the total number of documents, DELETED OR UNDELETED,
        in this reader.
        """

        raise NotImplementedError

    @abstractmethod
    def doc_count(self):
        """Returns the total number of UNDELETED documents in this reader.
        """

        return self.doc_count_all() - self.deleted_count()

    @abstractmethod
    def frequency(self, fieldname, text):
        """Returns the total number of instances of the given term in the
        collection.
        """
        raise NotImplementedError

    @abstractmethod
    def doc_frequency(self, fieldname, text):
        """Returns how many documents the given term appears in.
        """
        raise NotImplementedError

    @abstractmethod
    def field_length(self, fieldname):
        """Returns the total number of terms in the given field. This is used
        by some scoring algorithms.
        """
        raise NotImplementedError

    @abstractmethod
    def min_field_length(self, fieldname):
        """Returns the minimum length of the field across all documents. This
        is used by some scoring algorithms.
        """
        raise NotImplementedError

    @abstractmethod
    def max_field_length(self, fieldname):
        """Returns the minimum length of the field across all documents. This
        is used by some scoring algorithms.
        """
        raise NotImplementedError

    @abstractmethod
    def doc_field_length(self, docnum, fieldname, default=0):
        """Returns the number of terms in the given field in the given
        document. This is used by some scoring algorithms.
        """
        raise NotImplementedError

    def first_id(self, fieldname, text):
        """Returns the first ID in the posting list for the given term. This
        may be optimized in certain backends.
        """

        text = self._text_to_bytes(fieldname, text)
        p = self.postings(fieldname, text)
        if p.is_active():
            return p.id()
        raise TermNotFound((fieldname, text))

    def iter_postings(self):
        """Low-level method, yields all postings in the reader as
        ``(fieldname, text, docnum, weight, valuestring)`` tuples.
        """

        for fieldname, btext in self.all_terms():
            m = self.postings(fieldname, btext)
            while m.is_active():
                yield (fieldname, btext, m.id(), m.weight(), m.value())
                m.next()

    @abstractmethod
    def postings(self, fieldname, text, scorer=None):
        """Returns a :class:`~whoosh.matching.Matcher` for the postings of the
        given term.

        >>> pr = reader.postings("content", "render")
        >>> pr.skip_to(10)
        >>> pr.id
        12

        :param fieldname: the field name or field number of the term.
        :param text: the text of the term.
        :rtype: :class:`whoosh.matching.Matcher`
        """

        raise NotImplementedError

    @abstractmethod
    def has_vector(self, docnum, fieldname):
        """Returns True if the given document has a term vector for the given
        field.
        """
        raise NotImplementedError

    @abstractmethod
    def vector(self, docnum, fieldname, format_=None):
        """Returns a :class:`~whoosh.matching.Matcher` object for the
        given term vector.

        >>> docnum = searcher.document_number(path=u'/a/b/c')
        >>> v = searcher.vector(docnum, "content")
        >>> v.all_as("frequency")
        [(u"apple", 3), (u"bear", 2), (u"cab", 2)]

        :param docnum: the document number of the document for which you want
            the term vector.
        :param fieldname: the field name or field number of the field for which
            you want the term vector.
        :rtype: :class:`whoosh.matching.Matcher`
        """
        raise NotImplementedError

    def vector_as(self, astype, docnum, fieldname):
        """Returns an iterator of (termtext, value) pairs for the terms in the
        given term vector. This is a convenient shortcut to calling vector()
        and using the Matcher object when all you want are the terms and/or
        values.

        >>> docnum = searcher.document_number(path=u'/a/b/c')
        >>> searcher.vector_as("frequency", docnum, "content")
        [(u"apple", 3), (u"bear", 2), (u"cab", 2)]

        :param docnum: the document number of the document for which you want
            the term vector.
        :param fieldname: the field name or field number of the field for which
            you want the term vector.
        :param astype: a string containing the name of the format you want the
            term vector's data in, for example "weights".
        """

        vec = self.vector(docnum, fieldname)
        if astype == "weight":
            while vec.is_active():
                yield (vec.id(), vec.weight())
                vec.next()
        else:
            format_ = self.schema[fieldname].format
            decoder = format_.decoder(astype)
            while vec.is_active():
                yield (vec.id(), decoder(vec.value()))
                vec.next()

    def has_word_graph(self, fieldname):
        """Returns True if the given field has a "word graph" associated with
        it, allowing suggestions for correcting mis-typed words and fast fuzzy
        term searching.
        """

        return False

    def word_graph(self, fieldname):
        """Returns the root :class:`whoosh.fst.Node` for the given
        field, if the field has a stored word graph (otherwise raises an
        exception). You can check whether a field has a word graph using
        :meth:`IndexReader.has_word_graph`.
        """

        raise KeyError

    def corrector(self, fieldname):
        """Returns a :class:`whoosh.spelling.Corrector` object that suggests
        corrections based on the terms in the given field.
        """

        from whoosh.spelling import ReaderCorrector

        return ReaderCorrector(self, fieldname)

    def terms_within(self, fieldname, text, maxdist, prefix=0):
        """Returns a generator of words in the given field within ``maxdist``
        Damerau-Levenshtein edit distance of the given text.

        Important: the terms are returned in **no particular order**. The only
        criterion is that they are within ``maxdist`` edits of ``text``. You
        may want to run this method multiple times with increasing ``maxdist``
        values to ensure you get the closest matches first. You may also have
        additional information (such as term frequency or an acoustic matching
        algorithm) you can use to rank terms with the same edit distance.

        :param maxdist: the maximum edit distance.
        :param prefix: require suggestions to share a prefix of this length
            with the given word. This is often justifiable since most
            misspellings do not involve the first letter of the word.
            Using a prefix dramatically decreases the time it takes to generate
            the list of words.
        :param seen: an optional set object. Words that appear in the set will
            not be yielded.
        """

        fieldobj = self.schema[fieldname]
        for btext in self.expand_prefix(fieldname, text[:prefix]):
            word = fieldobj.from_bytes(btext)
            k = distance(word, text, limit=maxdist)
            if k <= maxdist:
                yield word

    def most_frequent_terms(self, fieldname, number=5, prefix=''):
        """Returns the top 'number' most frequent terms in the given field as a
        list of (frequency, text) tuples.
        """

        gen = ((terminfo.weight(), text) for text, terminfo
               in self.iter_prefix(fieldname, prefix))
        return nlargest(number, gen)

    def most_distinctive_terms(self, fieldname, number=5, prefix=''):
        """Returns the top 'number' terms with the highest `tf*idf` scores as
        a list of (score, text) tuples.
        """

        N = float(self.doc_count())
        gen = ((terminfo.weight() * log(N / terminfo.doc_frequency()), text)
               for text, terminfo in self.iter_prefix(fieldname, prefix))
        return nlargest(number, gen)

    def leaf_readers(self):
        """Returns a list of (IndexReader, docbase) pairs for the child readers
        of this reader if it is a composite reader. If this is not a composite
        reader, it returns `[(self, 0)]`.
        """

        return [(self, 0)]

    def supports_caches(self):
        return False

    def has_column(self, fieldname):
        return False

    def column_reader(self, fieldname):
        raise NotImplementedError


# Segment-based reader

class SegmentReader(IndexReader):
    def __init__(self, storage, schema, segment, generation=None, codec=None):
        self.schema = schema
        self.is_closed = False

        self._storage = storage
        self._segment = segment
        self._segid = self._segment.segment_id()
        self._gen = generation

        # self.files is a storage object from which to load the segment files.
        # This is different from the general storage (which will be used for
        # cahces) if the segment is in a compound file.
        if segment.is_compound():
            # Open the compound file as a storage object
            files = segment.open_compound_file(storage)
            # Use an overlay here instead of just the compound storage, in rare
            # circumstances a segment file may be added after the segment is
            # written
            self._files = OverlayStorage(files, self._storage)
        else:
            self._files = storage

        # Get subreaders from codec
        self._codec = codec if codec else segment.codec()
        self._terms = self._codec.terms_reader(self._files, segment)
        self._perdoc = self._codec.per_document_reader(self._files, segment)
        self._graph = None  # Lazy open with self._get_graph()

    def _get_graph(self):
        if not self._graph:
            self._graph = self._codec.graph_reader(self._files, self._segment)
        return self._graph

    def has_deletions(self):
        return self._perdoc.has_deletions()

    def doc_count(self):
        return self._perdoc.doc_count()

    def doc_count_all(self):
        return self._perdoc.doc_count_all()

    def is_deleted(self, docnum):
        return self._perdoc.is_deleted(docnum)

    def generation(self):
        return self._gen

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self._storage,
                               self._segment)

    def __contains__(self, term):
        fieldname, text = term
        if fieldname not in self.schema:
            return False
        text = self._text_to_bytes(fieldname, text)
        return (fieldname, text) in self._terms

    def close(self):
        self._terms.close()
        self._perdoc.close()
        if self._graph:
            self._graph.close()

        # It's possible some weird codec that doesn't use storage might have
        # passed None instead of a storage object
        if self._files:
            self._files.close()

        self.is_closed = True

    def stored_fields(self, docnum):
        assert docnum >= 0
        schema = self.schema
        sfs = self._perdoc.stored_fields(docnum)
        # Double-check with schema to filter out removed fields
        return dict(item for item in iteritems(sfs) if item[0] in schema)

    # Delegate doc methods to the per-doc reader

    def all_doc_ids(self):
        return self._perdoc.all_doc_ids()

    def iter_docs(self):
        return self._perdoc.iter_docs()

    def all_stored_fields(self):
        return self._perdoc.all_stored_fields()

    def field_length(self, fieldname):
        return self._perdoc.field_length(fieldname)

    def min_field_length(self, fieldname):
        return self._perdoc.min_field_length(fieldname)

    def max_field_length(self, fieldname):
        return self._perdoc.max_field_length(fieldname)

    def doc_field_length(self, docnum, fieldname, default=0):
        return self._perdoc.doc_field_length(docnum, fieldname, default)

    def has_vector(self, docnum, fieldname):
        return self._perdoc.has_vector(docnum, fieldname)

    #

    def _test_field(self, fieldname):
        if fieldname not in self.schema:
            raise TermNotFound("No field %r" % fieldname)
        if self.schema[fieldname].format is None:
            raise TermNotFound("Field %r is not indexed" % fieldname)

    def all_terms(self):
        schema = self.schema
        return ((fieldname, text) for fieldname, text in self._terms.terms()
                if fieldname in schema)

    def terms_from(self, fieldname, prefix):
        self._test_field(fieldname)
        prefix = self._text_to_bytes(fieldname, prefix)
        schema = self.schema
        return ((fname, text) for fname, text
                in self._terms.terms_from(fieldname, prefix)
                if fname in schema)

    def term_info(self, fieldname, text):
        self._test_field(fieldname)
        text = self._text_to_bytes(fieldname, text)
        try:
            return self._terms.term_info(fieldname, text)
        except KeyError:
            raise TermNotFound("%s:%r" % (fieldname, text))

    def expand_prefix(self, fieldname, prefix):
        self._test_field(fieldname)
        prefix = self._text_to_bytes(fieldname, prefix)
        return IndexReader.expand_prefix(self, fieldname, prefix)

    def lexicon(self, fieldname):
        self._test_field(fieldname)
        return IndexReader.lexicon(self, fieldname)

    def __iter__(self):
        schema = self.schema
        return ((term, terminfo) for term, terminfo in self._terms.items()
                if term[0] in schema)

    def iter_from(self, fieldname, text):
        schema = self.schema
        self._test_field(fieldname)
        text = self._text_to_bytes(fieldname, text)
        for term, terminfo in self._terms.items_from(fieldname, text):
            if term[0] not in schema:
                continue
            yield (term, terminfo)

    def frequency(self, fieldname, text):
        self._test_field(fieldname)
        text = self._text_to_bytes(fieldname, text)
        try:
            return self._terms.frequency(fieldname, text)
        except KeyError:
            return 0

    def doc_frequency(self, fieldname, text):
        self._test_field(fieldname)
        text = self._text_to_bytes(fieldname, text)
        try:
            return self._terms.doc_frequency(fieldname, text)
        except KeyError:
            return 0

    def postings(self, fieldname, text, scorer=None):
        from whoosh.matching.wrappers import FilterMatcher

        if fieldname not in self.schema:
            raise TermNotFound("No  field %r" % fieldname)
        text = self._text_to_bytes(fieldname, text)
        format_ = self.schema[fieldname].format
        matcher = self._terms.matcher(fieldname, text, format_, scorer=scorer)
        deleted = frozenset(self._perdoc.deleted_docs())
        if deleted:
            matcher = FilterMatcher(matcher, deleted, exclude=True)
        return matcher

    def vector(self, docnum, fieldname, format_=None):
        if fieldname not in self.schema:
            raise TermNotFound("No  field %r" % fieldname)
        vformat = format_ or self.schema[fieldname].vector
        if not vformat:
            raise Exception("No vectors are stored for field %r" % fieldname)
        return self._perdoc.vector(docnum, fieldname, vformat)

    # Graph methods

    def has_word_graph(self, fieldname):
        if fieldname not in self.schema:
            return False
        if not self.schema[fieldname].spelling:
            return False

        try:
            gr = self._get_graph()
        except NoGraphError:
            return False

        return gr.has_root(fieldname)

    def word_graph(self, fieldname):
        if not self.has_word_graph(fieldname):
            raise KeyError("No word graph for field %r" % fieldname)
        gr = self._get_graph()
        return fst.Node(gr, gr.root(fieldname))

    def terms_within(self, fieldname, text, maxdist, prefix=0):
        if not self.has_word_graph(fieldname):
            # This reader doesn't have a graph stored, use the slow method
            return IndexReader.terms_within(self, fieldname, text, maxdist,
                                            prefix=prefix)
        gr = self._get_graph()
        return fst.within(gr, text, k=maxdist, prefix=prefix,
                           address=self._graph.root(fieldname))

    # Column methods

    def has_column(self, fieldname):
        coltype = self.schema[fieldname].column_type
        return coltype and self._perdoc.has_column(fieldname)

    def column_reader(self, fieldname, column=None):
        fieldobj = self.schema[fieldname]
        column = column or fieldobj.column_type
        reader = self._perdoc.column_reader(fieldname, column)

        translate = fieldobj.from_column_value
        creader = columns.TranslatingColumnReader(reader, translate)
        return creader


# Fake IndexReader class for empty indexes

class EmptyReader(IndexReader):
    def __init__(self, schema):
        self.schema = schema

    def __contains__(self, term):
        return False

    def __iter__(self):
        return iter([])

    def all_terms(self):
        return iter([])

    def term_info(self, fieldname, text):
        raise TermNotFound((fieldname, text))

    def iter_from(self, fieldname, text):
        return iter([])

    def iter_field(self, fieldname):
        return iter([])

    def iter_prefix(self, fieldname):
        return iter([])

    def lexicon(self, fieldname):
        return iter([])

    def has_deletions(self):
        return False

    def is_deleted(self, docnum):
        return False

    def stored_fields(self, docnum):
        raise KeyError("No document number %s" % docnum)

    def all_stored_fields(self):
        return iter([])

    def doc_count_all(self):
        return 0

    def doc_count(self):
        return 0

    def frequency(self, fieldname, text):
        return 0

    def doc_frequency(self, fieldname, text):
        return 0

    def field_length(self, fieldname):
        return 0

    def min_field_length(self, fieldname):
        return 0

    def max_field_length(self, fieldname):
        return 0

    def doc_field_length(self, docnum, fieldname, default=0):
        return default

    def postings(self, fieldname, text, scorer=None):
        raise TermNotFound("%s:%r" % (fieldname, text))

    def has_vector(self, docnum, fieldname):
        return False

    def vector(self, docnum, fieldname, format_=None):
        raise KeyError("No document number %s" % docnum)

    def most_frequent_terms(self, fieldname, number=5, prefix=''):
        return iter([])

    def most_distinctive_terms(self, fieldname, number=5, prefix=None):
        return iter([])


# Multisegment reader class

class MultiReader(IndexReader):
    """Do not instantiate this object directly. Instead use Index.reader().
    """

    def __init__(self, readers, generation=None):
        self.readers = readers
        self._gen = generation
        self.schema = None
        if readers:
            self.schema = readers[0].schema

        self.doc_offsets = []
        self.base = 0
        for r in self.readers:
            self.doc_offsets.append(self.base)
            self.base += r.doc_count_all()

        self.is_closed = False

    def _document_segment(self, docnum):
        return max(0, bisect_right(self.doc_offsets, docnum) - 1)

    def _segment_and_docnum(self, docnum):
        segmentnum = self._document_segment(docnum)
        offset = self.doc_offsets[segmentnum]
        return segmentnum, docnum - offset

    def is_atomic(self):
        return False

    def leaf_readers(self):
        return zip_(self.readers, self.doc_offsets)

    def add_reader(self, reader):
        self.readers.append(reader)
        self.doc_offsets.append(self.base)
        self.base += reader.doc_count_all()

    def close(self):
        for d in self.readers:
            d.close()
        self.is_closed = True

    def generation(self):
        return self._gen

    def format(self, fieldname):
        for r in self.readers:
            fmt = r.format(fieldname)
            if fmt is not None:
                return fmt

    def vector_format(self, fieldname):
        for r in self.readers:
            vfmt = r.vector_format(fieldname)
            if vfmt is not None:
                return vfmt

    # Term methods

    def __contains__(self, term):
        return any(r.__contains__(term) for r in self.readers)

    def _merge_terms(self, iterlist):
        # Merge-sorts terms coming from a list of term iterators.

        # Create a map so we can look up each iterator by its id() value
        itermap = {}
        for it in iterlist:
            itermap[id(it)] = it

        # Fill in the list with the head term from each iterator.

        current = []
        for it in iterlist:
            try:
                term = next(it)
            except StopIteration:
                continue
            current.append((term, id(it)))
        # Number of active iterators
        active = len(current)

        # If only one iterator is active, just yield from it and return
        if active == 1:
            term, itid = current[0]
            it = itermap[itid]
            yield term
            for term in it:
                yield term
            return

        # Otherwise, do a streaming heap sort of the terms from the iterators
        heapify(current)
        while active:
            # Peek at the first term in the sorted list
            term = current[0][0]

            # Re-iterate on all items in the list that have that term
            while active and current[0][0] == term:
                it = itermap[current[0][1]]
                try:
                    nextterm = next(it)
                    heapreplace(current, (nextterm, id(it)))
                except StopIteration:
                    heappop(current)
                    active -= 1

            # Yield the term
            yield term

    def all_terms(self):
        return self._merge_terms([r.all_terms() for r in self.readers])

    def terms_from(self, fieldname, prefix):
        return self._merge_terms([r.terms_from(fieldname, prefix)
                                  for r in self.readers])

    def term_info(self, fieldname, text):
        term = (fieldname, text)

        # Get the term infos for the sub-readers containing the term
        tis = [(r.term_info(fieldname, text), offset) for r, offset
               in zip_(self.readers, self.doc_offsets) if term in r]

        # If only one reader had the term, return its terminfo with the offset
        # added
        if not tis:
            raise TermNotFound(term)
        elif len(tis) == 1:
            ti, offset = tis[0]
            ti._minid += offset
            ti._maxid += offset
            return ti

        # Combine the various statistics
        w = sum(ti.weight() for ti, _ in tis)
        df = sum(ti.doc_frequency() for ti, _ in tis)
        ml = min(ti.min_length() for ti, _ in tis)
        xl = max(ti.max_length() for ti, _ in tis)
        xw = max(ti.max_weight() for ti, _ in tis)

        # For min and max ID, we need to add the doc offsets
        mid = min(ti.min_id() + offset for ti, offset in tis)
        xid = max(ti.max_id() + offset for ti, offset in tis)

        return TermInfo(w, df, ml, xl, xw, mid, xid)

    def frequency(self, fieldname, text):
        return sum(r.frequency(fieldname, text) for r in self.readers)

    def doc_frequency(self, fieldname, text):
        return sum(r.doc_frequency(fieldname, text) for r in self.readers)

    def postings(self, fieldname, text, scorer=None):
        postreaders = []
        docoffsets = []
        term = (fieldname, text)

        for i, r in enumerate(self.readers):
            if term in r:
                offset = self.doc_offsets[i]

                # Get a posting reader for the term and add it to the list
                pr = r.postings(fieldname, text, scorer=scorer)
                postreaders.append(pr)
                docoffsets.append(offset)

        if not postreaders:
            raise TermNotFound(fieldname, text)
        else:
            return MultiMatcher(postreaders, docoffsets)

    def first_id(self, fieldname, text):
        for i, r in enumerate(self.readers):
            try:
                id = r.first_id(fieldname, text)
            except (KeyError, TermNotFound):
                pass
            else:
                if id is None:
                    raise TermNotFound((fieldname, text))
                else:
                    return self.doc_offsets[i] + id

        raise TermNotFound((fieldname, text))

    # Deletion methods

    def has_deletions(self):
        return any(r.has_deletions() for r in self.readers)

    def is_deleted(self, docnum):
        segmentnum, segmentdoc = self._segment_and_docnum(docnum)
        return self.readers[segmentnum].is_deleted(segmentdoc)

    def stored_fields(self, docnum):
        segmentnum, segmentdoc = self._segment_and_docnum(docnum)
        return self.readers[segmentnum].stored_fields(segmentdoc)

    # Per doc methods

    def all_stored_fields(self):
        for reader in self.readers:
            for result in reader.all_stored_fields():
                yield result

    def doc_count_all(self):
        return sum(dr.doc_count_all() for dr in self.readers)

    def doc_count(self):
        return sum(dr.doc_count() for dr in self.readers)

    def field_length(self, fieldname):
        return sum(dr.field_length(fieldname) for dr in self.readers)

    def min_field_length(self, fieldname):
        return min(r.min_field_length(fieldname) for r in self.readers)

    def max_field_length(self, fieldname):
        return max(r.max_field_length(fieldname) for r in self.readers)

    def doc_field_length(self, docnum, fieldname, default=0):
        segmentnum, segmentdoc = self._segment_and_docnum(docnum)
        reader = self.readers[segmentnum]
        return reader.doc_field_length(segmentdoc, fieldname, default=default)

    def has_vector(self, docnum, fieldname):
        segmentnum, segmentdoc = self._segment_and_docnum(docnum)
        return self.readers[segmentnum].has_vector(segmentdoc, fieldname)

    def vector(self, docnum, fieldname, format_=None):
        segmentnum, segmentdoc = self._segment_and_docnum(docnum)
        return self.readers[segmentnum].vector(segmentdoc, fieldname)

    def vector_as(self, astype, docnum, fieldname):
        segmentnum, segmentdoc = self._segment_and_docnum(docnum)
        return self.readers[segmentnum].vector_as(astype, segmentdoc,
                                                  fieldname)

    # Graph methods

    def has_word_graph(self, fieldname):
        return any(r.has_word_graph(fieldname) for r in self.readers)

    def word_graph(self, fieldname):
        from whoosh.fst import UnionNode
        from whoosh.util import make_binary_tree

        if not self.has_word_graph(fieldname):
            raise Exception("No word graph for field %r" % fieldname)

        graphs = [r.word_graph(fieldname) for r in self.readers
                  if r.has_word_graph(fieldname)]
        if len(graphs) == 0:
            raise KeyError("No readers have graph for %r" % fieldname)
        if len(graphs) == 1:
            return graphs[0]
        return make_binary_tree(UnionNode, graphs)

    def terms_within(self, fieldname, text, maxdist, prefix=0):
        tset = set()
        for r in self.readers:
            tset.update(r.terms_within(fieldname, text, maxdist,
                                       prefix=prefix))
        return tset

    # Column methods

    def has_column(self, fieldname):
        return any(r.has_column(fieldname) for r in self.readers)

    def column_reader(self, fieldname):
        column = self.schema[fieldname].column_type
        if not column:
            raise Exception("Field %r has no column type" % (fieldname,))
        default = column.default_value()
        doccount = self.doc_count_all()

        creaders = []
        for r in self.readers:
            if r.has_column(fieldname):
                creaders.append(r.column_reader(fieldname))
            else:
                creaders.append(columns.EmptyColumnReader(default, doccount))

        return columns.MultiColumnReader(creaders)
