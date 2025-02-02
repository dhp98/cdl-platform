import json
import os
import re
import time
from functools import wraps
from urllib.parse import urlparse, urldefrag, quote
from collections import defaultdict

import jwt
import bleach
from bson import ObjectId
from flask import request, current_app

from app.helpers import response
from app.helpers.status import Status
from app.models.users import Users

import validators


def validate_submission(highlighted_text, explanation, source_url=None):
    """
    Checks to make sure submitted text is not a URL!
    """
    if validators.url(highlighted_text) or validators.url(explanation):
        return False, "Error: The highlighted text or explanation should not be a URL"

    # check to make sure source url is not on wrong page
    if source_url != None:
        if not validators.url(source_url):
            return False, "Error: The URL is invalid: " + source_url
        forbidden_URLs = ["chrome://"]
        for url in forbidden_URLs:
            if url in source_url:
                return False, "Error: You cannot submit content on URL " + source_url

    char_max_desc = 10000
    word_max_desc = 1000

    char_max_title = 1000
    word_max_title = 100

    # cap highlighted text, explanation length
    if highlighted_text and (len(highlighted_text) > char_max_desc or len(highlighted_text.split()) > word_max_desc):
        return False, "Highlighted text is too long. Please limit to 1000 words or 10,000 characters"
    if explanation and (len(explanation) > char_max_title or len(explanation.split()) > word_max_title):
        return False, "Title is too long. Please limit to 100 words or 1000 characters"

    return True, "Validation successful"


def format_time_for_display(timestamp):
	# hack to compute time ago up to years
	current_time = int(time.time())
	time_ago = current_time - int(timestamp)
	time_map = {"minutes": 60,
				"hours": 60,
				"days": 24,
				"months": 30,
				"years": 12}
	display_text = "seconds"
	for time_str in time_map:
		if time_ago / time_map[time_str] < 1:
			break
		else:
			time_ago = time_ago / time_map[time_str]
			display_text = time_str
	time_ago = str(int(time_ago))
	if time_ago == "1":
		display_text = display_text[:-1]
	
	return time_ago + " " + display_text + " ago"
	


def extract_payload(request, fields):
	try:
		payload = {field: request.form.get(field) for field in fields}
		if None in [value for value in payload.values()] and request.data:
			payload = {}
			request_data = json.loads(request.data.decode("utf-8"))
			for field in fields:
				payload[field] = request_data[field]
		return payload if None not in [value for value in payload.values()] else None
	except:
		return None


def token_required(f):
	"""
	This wrapper ensures that any incoming request is coming from a valid account. The cookie is sent via the "Authorization" header, and
	it is a JWT token that contains the ObjectID of the user.
	Arguments:
		f : (function) : whatever function is being wrapped.

	Returns:
		If the token is valid, then the passed function with the current user and any other params passed to the initial function.
		If the token is invalid, then a 403 error with an error message.
	"""

	@wraps(f)
	def decorator(*args, **kwargs):
		token = None
		if 'Authorization' in request.headers:
			token = request.headers['Authorization']
		if not token:
			return response.error("Valid token is missing", Status.BAD_REQUEST)
		try:
			data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
			cdl_users = Users()
			current_user = cdl_users.find_one({"_id": ObjectId(data["id"])})
			assert current_user != None
		except Exception as e:
			print("Error: ", e)
			return response.error("Token is invalid", Status.NOT_FOUND)

		return f(current_user, *args, **kwargs)

	return decorator


def build_result_hash(rank, submission_id, search_id):
	"""
	Helper function for building the result hash of format rank_submissionID_searchID
	"""
	result_hash = str(rank) + "_" + submission_id + "_" + search_id
	return result_hash


def build_display_url(url):
	"""
	Helper function for building the display URL. Replaces "/" with " > ". Ignores last slash
	"""
	parsed_url = urlparse(url)
	path = parsed_url.path
	if path and path[-1] == "/":
		path = path[:-1]
	display_url = parsed_url.netloc + re.sub("/", " > ", path)
	return display_url


def build_redirect_url(url, result_hash, highlighted_text, method="search"):
	"""
	Helper function for building the redirect URL (so clicks are logged on our backend)
	Arguments:
		url : string : the URL that the submission points to
		result_hash : string : the rank_submissionID_searchID from above function
		highlighted_text : string : the highlighted text of the submission
	"""
	# if method is anything other than 'search', assume redirection call is made from rec and not search (since method = recent/trending/unseen etc)
	if(method!="search"): method="recommendation"

	redirect_url = os.environ["api_url"] + ":" + os.environ["api_port"] + "/api/redirect?"
	redirect_url += "method="+ str(method)
	redirect_url += "&hash=" + result_hash
	url, fragment = urldefrag(url)
	# handling edge cases
	if "pdf" in url and fragment != "":  # for proxies
		redirect_url += "&redirect_url=" + url + "#" + fragment
	elif "youtube" in url:
		redirect_url += "&redirect_url=" + quote(url)
	else:
		redirect_url += "&redirect_url=" + url

	return redirect_url


def create_page(hits, communities, toggle_display="highlight"):
    """
	Helper function for formatting raw elastic results.
	Arguments:
		hits : (dict): json raw output from elastic
		communities : (dict) : the user's communities
        toggle_display : (str) : highlight to show matched highlighted text, preview to show best preview
	Returns:
		return_obj : (list) : a list of formatted submissions for frontend display
							Note that result_hash and redirect_url will be empty (need to hydrate)
	"""

    return_obj = []
    for i, hit in enumerate(hits):
        result = {
            "redirect_url": None,
            "display_url": None,
            "orig_url": None,
            "submission_id": None,
            "result_hash": None,
            "highlighted_text": None,
            "explanation": None,
            "score": hit.get("_score", 0),
            "time": "",
            "communities_part_of": []
        }

        if "webpage" in hit["_source"]:
            result["explanation"] = hit["_source"]["webpage"]["metadata"].get("title", "No Title Available")

            possible_matches = []
            if "highlight" in hit and toggle_display == "highlight":
                possible_matches = hit["highlight"].get("webpage.metadata.description", [])
                if not possible_matches:
                     possible_matches = hit["highlight"].get("webpage.metadata.h1", [])
                if not possible_matches:
                    # in case there are a lot of paragraphs
                    possible_matches = hit["highlight"].get("webpage.all_paragraphs", [])[:10]

                description = " .... ".join(possible_matches)
            else:
                description = None

            if not description:
                description = hit["_source"]["webpage"]["metadata"].get("description", None)
            if not description:
                description = hit["_source"]["webpage"]["metadata"].get("h1", None)
            if not description:
                description = "No Preview Available"
            result["highlighted_text"] = description

        else:
            result["explanation"] = hit["_source"].get("explanation", "No Explanation Available")




            description = " .... ".join(hit["highlight"].get("highlighted_text", [])) if hit.get("highlight", None) and toggle_display == "highlight" else hit["_source"].get("highlighted_text", None)
            if not description:
                description ="No Preview Available"
            result["highlighted_text"] = description

        # possible that returns additional communities?
        result["communities_part_of"] = {community_id: communities[community_id] for community_id in
                                         hit["_source"].get("communities", []) if community_id in communities}

        result["submission_id"] = str(hit["_id"])

        if "time" in hit["_source"]:
            formatted_time = "Submitted " + format_time_for_display(hit["_source"]["time"])
            result["time"] = formatted_time
        elif "scrape_time" in hit["_source"]:
            formatted_time = "Indexed " + format_time_for_display(hit["_source"]["scrape_time"])
            result["time"] = formatted_time


        # Display URL
        url = hit["_source"].get("source_url", "")
        display_url = build_display_url(url)
        result["display_url"] = display_url
        result["orig_url"] = url

        return_obj.append(result)
    return return_obj

def hydrate_with_hash_url(results, search_id, page=0, page_size=10, method="search"):
    """
	Helper function hydrating with result hash and url.
	This is separate from create_page because neural reranking happens in between.
	Arguments:
		results : (list): output of create_pages
		search_id : (string) : the id of the search
		page : (int, default=0) : current page
		page_size : (int, default=10) : the size of each page
	Returns:
		pages : (list) : a reranked pages	
	"""
    for i, result in enumerate(results):
        # result hash
        result_hash = build_result_hash(str((page * page_size) + i), str(result["submission_id"]), str(search_id))

        result["result_hash"] = result_hash

        # build the redirect URL for clicks
        redirect_url = build_redirect_url(result["orig_url"], result_hash, result["highlighted_text"], method)
        result["redirect_url"] = redirect_url
    return results


def hydrate_with_hashtags(results):
    for result in results:
        # add the hashtags
        result["hashtags"] = []
        hashtags_explanation = [x for x in result["explanation"].split() if len(x) > 1 and x[0] == "#"]
        hashtags_ht = [x for x in result["highlighted_text"].split() if len(x) > 1 and x[0] == "#"]
        hashtags = list(set(hashtags_explanation + hashtags_ht))
        # remove mark in case hashtag is in body

        hashtags = [re.sub("<mark>", "", x) for x in hashtags]
        hashtags = [re.sub("</mark>", "", x) for x in hashtags]

        result["hashtags"] = hashtags
    return results

def diversify(pages, topn=10):
    """
    Slightly reorders pages to make top n domain-diverse.
    """
    pass

def standardize_url(url):
    # remove fragment from query
    if "#" in url:
        url = url.split("#")[0]
    return url

def extract_hashtags(text):
      hashtags = [x for x in text.split() if len(x) > 1 and x[0] == "#"]
      return hashtags


def deduplicate(pages):
    """
    Helper function to remove all duplicate pages. Saves them as "children" in top-rated page.
    """
    map_pages = defaultdict(list)

    for page in pages:
        url = page["orig_url"].split("#")[0]
        map_pages[url].append(page)

    for key in map_pages.keys():
        map_pages[key][0]["children"] = map_pages[key][1:11] if len(map_pages[key]) > 10 else map_pages[key][1:]

    return [p[0] for p in map_pages.values()]

def combine_pages(submissions_pages, webpages_index_pages):

    # Building an inverted index to map orig_url to index using the submissions_pages list
    subpgs_url_to_id = {}
    for i, submission_page in enumerate(submissions_pages):
        subpgs_url_to_id[submission_page["orig_url"]] = i

    # Using the orig_url_to_idx_map to see if there is an in entry in webpages_index_pages to update score
    for webpage in webpages_index_pages:
        if subpgs_url_to_id.get(webpage["orig_url"]):
            i = subpgs_url_to_id.get(webpage["orig_url"])
            submissions_pages[i]["score"] = submissions_pages[i]["score"] + webpage["score"]
    
    return submissions_pages + webpages_index_pages

def sanitize_input(input_data):
	"""
	Function to sanitize input data and remove any malicious code string to prevent from security threats
	like XSS attacks.
	
	return: Sanitized input data
	"""
	if input_data and type(input_data)==str:
		try:
			# Define a list of allowed HTML tags and attributes
			allowed_tags = ['mark']
			sanitized_data = bleach.clean(input_data, tags=allowed_tags)
			return sanitized_data

		except Exception as e :
			print(f"Error occured while sanitizing input data {input_data}: ", e)
	else:
		print(f"Cannot Sanitize input data {input_data}")

	return input_data