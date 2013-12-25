import pylab
import json
import numpy as np
import os
import csv
from gensim.models.word2vec import *
import nltk
from scipy.stats import scoreatpercentile
from nltk.stem import PorterStemmer
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import proj3d
import hungarian
import heapq

FIELD_TYPE_NUMERIC = "numeric"
FIELD_TYPE_TEXT = "text"
STEMMER = PorterStemmer()

def compute_percentile(value, cutoffs):
	if value < cutoffs[0]:
		return 0.0

	for i, cutoff in enumerate(cutoffs):
		if value < cutoff:
			return 100 * (float(i)/(len(cutoffs)))
			break
	return 100.0

def read_csv_as_json(csv_path, callback, updateStr = None):
	fields = None
	num = 0
	with open(csv_path, 'rU') as csvfile:
		reader = csv.reader(csvfile, delimiter=',', quotechar='"')
		for row in reader:
			if not fields:
				fields = {}
				for i, field in enumerate(row):
					fields[field] = i
			else:
				keyValue = {}
				for field in fields:
					value = row[fields[field]]
					keyValue[field] = value
				callback(keyValue)
				num += 1
				if (updateStr and num % 1000 == 0):
					print updateStr + " " + str(num)

def compute_schema_percentiles(schema):
	values = {}
	csv_path = schema["dataset_path"]
	schemaFields = schema["fields"]
	for field in schemaFields:
		if schemaFields[field]["type"] == FIELD_TYPE_NUMERIC:
			values[field] = []
	
	def processRow(row, schemaFields, values):
		# cache the values for numerics
		for field in schemaFields:
			if schemaFields[field]["type"] == FIELD_TYPE_NUMERIC:
				values[field].append(float(row[field]));

	read_csv_as_json(csv_path, lambda x: processRow(x, schemaFields, values))

	# compute percentiles
	for field in values:
		theList = np.array(values[field])
		theMedian = np.median(theList)
		oneSidedList = theList[:]
		oneSidedList[theList < theMedian] = 2*theMedian - theList[theList < theMedian]
		percentiles = []
		for i in xrange(0, 10):
			percentile =  10 * i
			a = scoreatpercentile(oneSidedList, percentile) - theMedian;
			percentiles.append(a)
		schemaFields[field]["percentile"] = percentiles
	schema["fields"] = schemaFields
	return schema

def model_path(schema):
	return os.path.join(os.path.dirname(schema["dataset_path"]), os.path.basename(schema["dataset_path"].split(".")[0] + "_model.out"))

def weight_matrix_path(schema):
	return os.path.join(os.path.dirname(schema["dataset_path"]), os.path.basename(schema["dataset_path"].split(".")[0] + "_weight.out"))


# Document comparison algorithm
# Given  documents A,B and |A|<|B|
# assign an edge between all points in A to all points in B with weight = 1 / similarity(p1,p2)
# run hungarian algorithm to find maximal matching (this yields a subset of A's points)
# return sum of weights as score
def compare_docs(sentenceA, sentenceB, model):

	n = max(len(sentenceA), len(sentenceB))

	if(len(sentenceA) < n):
		shorter = sentenceA
		longer = sentenceB
	else:
		shorter = sentenceB
		longer = sentenceA

	# compute distance matrix from A => B
	mat = []
	for i in xrange(0, n):
		row = [0 for m in xrange(0,n)];
		for j in xrange(0,n):
			if(i >= len(shorter)):
				row[j] = 99999;
			else:
				try:
					row[j] = abs(1.0 / model.similarity(shorter[i], longer[j]))
				except:
					row[j] = 99999;
		mat.append(row)

	# run hungarian algorithm on cost matrix
	matches = hungarian.lap(mat)[0]
	# sum over minimal distance matching
	total = 0
	for i in xrange(0, len(shorter)):
		total += model.similarity(shorter[i], longer[matches[i]])
	return total

def run_point_cloud_search(positiveDoc, negativeDoc, schema, model, fieldsToCompare = None, numResults = 100):
	if not fieldsToCompare:
		fieldsToCompare = schema["fields"].keys()
	# convert to sentence if doc is not a sentence
	
	if type(positiveDoc) == type({}):
		docPositive = convert_json_to_sentence(schema, doc, fieldsToCompare);
	else:
		docPositive = positiveDoc

	if type(negativeDoc) == type({}):
		docNegative = convert_json_to_sentence(schema, doc, fieldsToCompare);
	else:
		docNegative = negativeDoc
	
	# build a search queue using heapq
	heap = []
	
	# scan through dataset
	def compareValue(docPositive, docNegative, docToCompare, heap, schema, fieldsToCompare, model):
		docB = convert_json_to_sentence(schema, docToCompare, fieldsToCompare)
		score = compare_docs(docPositive, docB, model)
		if(docNegative):
			score -= compare_docs(docNegative, docB, model)
		heapq.heappush(heap, (score,docToCompare))
		# pop the lowest score if we've gotten too many items
		if len(heap) > numResults:
			heapq.heappop(heap)

	read_csv_as_json(schema["dataset_path"], lambda x : compareValue(docPositive, docNegative, x, heap, schema, fieldsToCompare, model), "Searching...")
	ret = []
	for i in xrange(0, numResults):
		ret.append(heapq.heappop(heap))
	ret.reverse()
	return ret

def generate_field_features(schema, field, value):
	global STEMMER
	if(schema["fields"][field]["type"] == FIELD_TYPE_NUMERIC):
		return [field + "_" + str(compute_percentile(float(value), schema["fields"][field]["percentile"]))+"_percentile", field+"_"+str(value)+"_value"]
	else:
		return map(lambda x : STEMMER.stem(x.lower()) , nltk.word_tokenize(value))

def convert_json_to_sentence(schema, keyValues, fieldsToRead):
	features = []
	for field in fieldsToRead:
		value = keyValues[field]
		features += generate_field_features(schema,field, value)
	featuresNonUnicode = []
	for feature in features:
		try:
			raw = feature.encode('ascii', 'ignore')
			featuresNonUnicode.append(raw)
		except:
			continue
	return featuresNonUnicode

def train_model(schema, vectorSize, fieldsToRead = None):
	if not fieldsToRead:
		fieldsToRead = schema["fields"].keys()

	sentences = []
	# build sentences:
	print "Building Feature vectors..."
	csv_path = schema["dataset_path"]
	read_csv_as_json(csv_path, lambda x : sentences.append(convert_json_to_sentence(schema, x, fieldsToRead)), "Built")
	print "Generated " + str(len(sentences)) + " documents"
	print "Training Model..."
	modelPath = model_path(schema)
	weightMatrixPath = weight_matrix_path(schema)
	model = Word2Vec(sentences, size=vectorSize, window=5, min_count=5, workers=4)
	model.save(modelPath)
	model.save_word2vec_format(weightMatrixPath)
	print "Finished training"
	return model


json_data = open(sys.argv[1]).read()
schema = json.loads(json_data)
schema = compute_schema_percentiles(schema)

try:
	# try loading model
	model =  Word2Vec.load_word2vec_format(weight_matrix_path(schema), binary=False)
except:
	# otherwise compute it:
	model = train_model(schema, 50)

ret = run_point_cloud_search(["Reported_Minimum_100.0_percentile", "car"], ["bomb"], schema, model)
for r in ret:
	print r

"""
xs = []
ys = []
zs = []

fig = plt.figure()
ax = fig.add_subplot(111, projection='3d')
vectorList = map(lambda x : x.rstrip().split(' ')[0], open(weight_matrix_path(schema), "r").readlines())

print vectorList
words = map( lambda x : STEMMER.stem(x.lower()), vectorList[:100])
for word in words:
	try:
		w = model[word]
		xs.append(w[0])
		ys.append(w[1])
		zs.append(w[2])
	except:
		continue;
sc = ax.scatter(xs, ys, zs);

labels = []
for word in words:
	try:
		x2, y2, _ = proj3d.proj_transform(model[word][0],model[word][1],model[word][2], ax.get_proj())
		label = ax.annotate(word, xy = (x2, y2), xytext = (-20, 20), textcoords = 'offset points', ha = 'right', va = 'bottom', bbox = dict(boxstyle = 'round,pad=0.5', fc = 'yellow', alpha = 0.5), arrowprops = dict(arrowstyle = '->', connectionstyle = 'arc3,rad=0'))
		labels.append((label, model[word]))
	except:
		continue

def update_position(e):
	for labelArr in labels:
		x2, y2, _ = proj3d.proj_transform(labelArr[1][0], labelArr[1][1], labelArr[1][2], ax.get_proj())
		labelArr[0].xy = x2,y2
		labelArr[0].update_positions(fig.canvas.renderer)
		fig.canvas.draw()

fig.canvas.mpl_connect('button_release_event', update_position)
pylab.show()
plt.show()
"""